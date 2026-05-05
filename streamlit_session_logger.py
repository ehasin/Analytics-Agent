"""
streamlit_session_logger.py — Structured usage logging for the Streamlit Analytics Agent.

Backend: Neon (PostgreSQL) in production; SQLite at logs/usage.db for local dev.
Auto-detected: set NEON_DB_URL in st.secrets or environment to enable Neon.
Falls back to SQLite silently if the variable is absent or the connection fails.

Tables
──────
  app_sessions    one row per browser session (geo, device, usage counts)
  chat_logs       one row per conversation thread
  turns           one row per agent turn (structured fields + full raw_output JSONB)
  stage_traces    one row per pipeline stage per turn
  queries         one row per SQL query executed per turn

Privacy / GDPR
──────────────
  • IP addresses are NEVER stored — geo is resolved transiently and discarded.
  • City is logged only when it matches a major municipal area (pop ≥ ~200k).
    Smaller cities fall back to NULL; region + country are always logged.
  • User-Agent is parsed to OS / browser / device-type; the raw string is discarded.

Public API
──────────
  StreamlitSessionLogger(db_url=None)
    .ping()
    .create_session(session_id, *, timezone, city, region, country, country_code,
                    device_type, os, browser, started_at)
    .update_session(session_id, *, turns_delta=0, chats_delta=0, mode=None,
                    backend=None, last_active_at=None)
    .create_chat(chat_id, session_id, *, backend, started_at)
    .update_chat(chat_id, *, turn_count_delta=1, mode=None)
    .log_turn(turn_id, chat_id, session_id, *, turn_num, question_raw,
              question_resolved, mode, mode_control, turn_type, classification,
              total_seconds, turn_status, error, narrative, raw_output)
    .log_stage_traces(turn_id, traces)
    .log_queries(turn_id, queries)

  Helpers (module-level)
    resolve_geo(ip)           IP → {city, region, country, country_code, timezone}
    parse_user_agent(ua_str)  UA string → {device_type, os, browser}
    MAJOR_CITIES              frozenset of lowercase city names (pop ≥ ~200k)
"""

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── Major cities (pop ≥ ~200k) for privacy-safe city logging ────────────────
# City from ip-api.com is stored only if it appears here (case-insensitive).
# All other cities fall back to NULL — only region + country are logged.

MAJOR_CITIES: frozenset = frozenset({
    # ── USA ──
    "new york", "los angeles", "chicago", "houston", "phoenix", "philadelphia",
    "san antonio", "san diego", "dallas", "san jose", "austin", "jacksonville",
    "fort worth", "columbus", "charlotte", "indianapolis", "san francisco",
    "seattle", "denver", "nashville", "oklahoma city", "el paso", "washington",
    "las vegas", "louisville", "memphis", "portland", "baltimore", "milwaukee",
    "albuquerque", "tucson", "fresno", "sacramento", "mesa", "kansas city",
    "atlanta", "omaha", "colorado springs", "raleigh", "long beach", "minneapolis",
    "tampa", "arlington", "wichita", "bakersfield", "aurora", "anaheim",
    "riverside", "st. louis", "pittsburgh", "stockton", "cincinnati", "newark",
    "orlando", "plano", "henderson", "lincoln", "jersey city", "chandler",
    "st. petersburg", "laredo", "norfolk", "madison", "durham", "lubbock",
    "garland", "hialeah", "reno", "baton rouge", "irvine", "scottsdale",
    "fremont", "gilbert", "birmingham", "rochester", "richmond", "spokane",
    "des moines", "montgomery", "little rock", "amarillo", "grand rapids",
    "salt lake city", "tallahassee", "huntsville", "worcester", "knoxville",
    "oxnard", "providence", "tempe", "akron", "fort lauderdale", "glendale",
    "cape coral", "sioux falls", "jackson", "moreno valley", "shreveport",
    "fontana", "fayetteville", "augusta", "tacoma",
    # ── Canada ──
    "toronto", "montreal", "vancouver", "calgary", "edmonton", "ottawa",
    "winnipeg", "quebec city", "hamilton", "kitchener", "halifax", "saskatoon",
    "regina", "victoria", "windsor", "surrey", "brampton", "mississauga",
    # ── Mexico ──
    "mexico city", "guadalajara", "monterrey", "puebla", "tijuana", "leon",
    "juarez", "zapopan", "nezahualcoyotl", "chihuahua", "guadalupe", "merida",
    "san luis potosi", "aguascalientes", "hermosillo", "cancun", "culiacan",
    "acapulco", "mexicali", "ecatepec", "tlalnepantla",
    # ── Caribbean / Central America ──
    "havana", "santo domingo", "san juan", "port-au-prince", "guatemala city",
    "tegucigalpa", "san salvador", "managua", "san jose", "panama city",
    "kingston", "port of spain",
    # ── South America ──
    "sao paulo", "buenos aires", "rio de janeiro", "bogota", "lima", "santiago",
    "caracas", "quito", "la paz", "montevideo", "asuncion", "guayaquil",
    "medellin", "cali", "fortaleza", "belo horizonte", "manaus", "porto alegre",
    "recife", "brasilia", "salvador", "curitiba", "goiania", "maracaibo",
    "barquisimeto", "rosario", "cordoba", "mendoza", "la plata", "mar del plata",
    "barranquilla", "cartagena", "pereira", "cucuta", "santa cruz", "cochabamba",
    "bucaramanga", "campinas", "belem", "sorocaba", "maceio", "natal",
    "teresina", "campo grande", "joao pessoa", "sao luis",
    # ── Western Europe ──
    "london", "paris", "berlin", "madrid", "rome", "barcelona", "vienna",
    "warsaw", "hamburg", "budapest", "amsterdam", "rotterdam", "milan", "naples",
    "turin", "frankfurt", "stuttgart", "cologne", "dusseldorf", "munich",
    "leipzig", "dresden", "essen", "dortmund", "bremen", "hanover", "nuremberg",
    "dublin", "brussels", "antwerp", "ghent", "liege", "athens", "lisbon",
    "porto", "prague", "bratislava", "bucharest", "sofia", "zagreb", "belgrade",
    "sarajevo", "skopje", "tirana", "riga", "tallinn", "vilnius", "helsinki",
    "stockholm", "gothenburg", "malmo", "oslo", "bergen", "copenhagen", "aarhus",
    "zurich", "geneva", "bern", "basel", "lyon", "marseille", "nice", "toulouse",
    "strasbourg", "bordeaux", "nantes", "lille", "rennes", "leeds", "glasgow",
    "edinburgh", "sheffield", "bristol", "liverpool", "manchester", "birmingham",
    "seville", "zaragoza", "malaga", "murcia", "palma", "alicante", "bilbao",
    "krakow", "lodz", "wroclaw", "poznan", "gdansk", "lublin", "katowice",
    "thessaloniki", "palermo", "catania", "bari", "florence", "bologna", "genoa",
    "venice", "verona", "the hague", "utrecht", "eindhoven", "timisoara",
    "iasi", "cluj-napoca", "plovdiv", "varna", "nicosia", "reykjavik",
    # ── Eastern Europe / Russia / CIS ──
    "moscow", "saint petersburg", "novosibirsk", "yekaterinburg",
    "nizhny novgorod", "kazan", "chelyabinsk", "omsk", "samara",
    "rostov-on-don", "ufa", "krasnoyarsk", "perm", "voronezh", "volgograd",
    "saratov", "krasnodar", "tolyatti", "izhevsk", "tyumen", "barnaul",
    "vladivostok", "irkutsk", "yaroslavl", "khabarovsk", "novokuznetsk",
    "orenburg", "ryazan", "kyiv", "kharkiv", "odessa", "dnipro", "zaporizhia",
    "lviv", "donetsk", "minsk", "almaty", "astana", "tashkent", "baku",
    "tbilisi", "yerevan", "bishkek", "dushanbe", "ashgabat", "chisinau",
    # ── Middle East ──
    "istanbul", "ankara", "izmir", "bursa", "adana", "gaziantep", "konya",
    "mersin", "kayseri", "tehran", "mashhad", "isfahan", "karaj", "shiraz",
    "tabriz", "ahvaz", "qom", "baghdad", "basra", "mosul", "erbil", "dubai",
    "abu dhabi", "sharjah", "riyadh", "jeddah", "mecca", "medina", "doha",
    "kuwait city", "muscat", "amman", "beirut", "damascus", "aleppo", "sanaa",
    "aden", "tel aviv", "jerusalem", "haifa",
    # ── South Asia ──
    "mumbai", "delhi", "bangalore", "kolkata", "chennai", "hyderabad",
    "ahmedabad", "pune", "surat", "jaipur", "lucknow", "kanpur", "nagpur",
    "visakhapatnam", "indore", "thane", "bhopal", "patna", "vadodara",
    "ghaziabad", "ludhiana", "agra", "nashik", "faridabad", "meerut", "rajkot",
    "varanasi", "srinagar", "aurangabad", "amritsar", "coimbatore", "ranchi",
    "jabalpur", "gwalior", "vijayawada", "jodhpur", "madurai", "raipur", "kota",
    "chandigarh", "guwahati", "solapur", "mysore", "tiruchirappalli", "dhaka",
    "chittagong", "khulna", "rajshahi", "karachi", "lahore", "faisalabad",
    "rawalpindi", "gujranwala", "peshawar", "multan", "islamabad", "colombo",
    "kathmandu", "kabul", "kandahar", "herat",
    # ── East Asia ──
    "tokyo", "yokohama", "osaka", "nagoya", "sapporo", "kobe", "kyoto",
    "fukuoka", "kawasaki", "saitama", "hiroshima", "sendai", "kitakyushu",
    "seoul", "busan", "incheon", "daegu", "daejeon", "gwangju", "ulsan",
    "beijing", "shanghai", "guangzhou", "shenzhen", "chongqing", "tianjin",
    "wuhan", "chengdu", "nanjing", "xi'an", "dongguan", "hangzhou", "shenyang",
    "harbin", "suzhou", "qingdao", "jinan", "zhengzhou", "changsha", "kunming",
    "dalian", "xiamen", "hefei", "shijiazhuang", "urumqi", "fuzhou", "nanchang",
    "changchun", "taiyuan", "guiyang", "foshan", "wenzhou", "nanning", "wuxi",
    "ningbo", "hong kong", "macau", "taipei", "kaohsiung", "taichung", "tainan",
    "pyongyang", "ulaanbaatar",
    # ── Southeast Asia ──
    "bangkok", "chiang mai", "jakarta", "surabaya", "bandung", "medan",
    "semarang", "bekasi", "depok", "tangerang", "makassar", "palembang",
    "kuala lumpur", "penang", "johor bahru", "singapore", "manila",
    "quezon city", "caloocan", "davao", "cebu", "zamboanga",
    "ho chi minh city", "hanoi", "da nang", "haiphong", "can tho", "yangon",
    "mandalay", "naypyidaw", "phnom penh", "vientiane",
    # ── Oceania ──
    "sydney", "melbourne", "brisbane", "perth", "adelaide", "gold coast",
    "newcastle", "canberra", "wollongong", "auckland", "wellington",
    "christchurch", "hamilton", "port moresby",
    # ── Africa ──
    "cairo", "lagos", "kinshasa", "luanda", "dar es salaam", "khartoum",
    "abidjan", "alexandria", "johannesburg", "nairobi", "casablanca",
    "cape town", "durban", "addis ababa", "kumasi", "accra", "kampala",
    "maputo", "dakar", "lusaka", "harare", "bamako", "conakry", "ouagadougou",
    "lome", "cotonou", "kano", "ibadan", "abuja", "port harcourt", "benin city",
    "kaduna", "douala", "yaounde", "antananarivo", "mogadishu", "djibouti",
    "asmara", "tunis", "tripoli", "algiers", "rabat", "marrakech", "fez",
    "oran", "mombasa", "pretoria", "bloemfontein", "brazzaville", "libreville",
    "bangui", "ndjamena", "niamey", "nouakchott", "monrovia", "freetown",
    "kigali", "bujumbura", "goma", "lubumbashi", "mbuji-mayi", "kisangani",
    "windhoek", "gaborone", "lilongwe", "blantyre", "ndola", "bulawayo",
})


# ── Geo resolution (IP never stored) ────────────────────────────────────────

def resolve_geo(ip: str) -> dict:
    """Resolve IP to location. IP is used transiently and never stored.

    Returns dict with keys: city (None if small), region, country,
    country_code, timezone. All values may be None on failure.
    """
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return {}
    try:
        import requests as _req
        resp = _req.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "status,country,countryCode,regionName,city,timezone"},
            timeout=5,
        )
        data = resp.json()
        if data.get("status") != "success":
            return {}
        raw_city = (data.get("city") or "").strip()
        safe_city = raw_city if raw_city.lower() in MAJOR_CITIES else None
        return {
            "city": safe_city,
            "region": data.get("regionName") or None,
            "country": data.get("country") or None,
            "country_code": data.get("countryCode") or None,
            "timezone": data.get("timezone") or None,
        }
    except Exception:
        return {}


def parse_user_agent(ua_string: str) -> dict:
    """Parse User-Agent string to device_type / os / browser. Raw string discarded."""
    try:
        from user_agents import parse as _ua_parse
        ua = _ua_parse(ua_string or "")
        device_type = "mobile" if ua.is_mobile else "tablet" if ua.is_tablet else "desktop"
        return {
            "device_type": device_type,
            "os": ua.os.family or None,
            "browser": ua.browser.family or None,
        }
    except Exception:
        return {"device_type": None, "os": None, "browser": None}


# ── DDL ─────────────────────────────────────────────────────────────────────

_POSTGRES_DDL = """
CREATE TABLE IF NOT EXISTS app_sessions (
    session_id       TEXT PRIMARY KEY,
    started_at       TIMESTAMPTZ NOT NULL,
    last_active_at   TIMESTAMPTZ,
    timezone         TEXT,
    city             TEXT,
    region           TEXT,
    country          TEXT,
    country_code     TEXT,
    device_type      TEXT,
    os               TEXT,
    browser          TEXT,
    chat_count       INTEGER NOT NULL DEFAULT 0,
    total_turns      INTEGER NOT NULL DEFAULT 0,
    backend_used     TEXT,
    mode_retrieve_count  INTEGER NOT NULL DEFAULT 0,
    mode_explore_count   INTEGER NOT NULL DEFAULT 0,
    mode_reason_count    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_logs (
    chat_id          TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES app_sessions(session_id),
    started_at       TIMESTAMPTZ NOT NULL,
    backend          TEXT,
    turn_count       INTEGER NOT NULL DEFAULT 0,
    mode_retrieve_count  INTEGER NOT NULL DEFAULT 0,
    mode_explore_count   INTEGER NOT NULL DEFAULT 0,
    mode_reason_count    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id            TEXT PRIMARY KEY,
    chat_id            TEXT NOT NULL REFERENCES chat_logs(chat_id),
    session_id         TEXT NOT NULL REFERENCES app_sessions(session_id),
    turn_num           INTEGER NOT NULL,
    question_raw       TEXT,
    question_resolved  TEXT,
    mode               INTEGER,
    mode_control       TEXT,
    turn_type          TEXT,
    classification     TEXT,
    total_seconds      REAL,
    turn_status        TEXT,
    error              TEXT,
    narrative_excerpt  TEXT,
    raw_output         JSONB,
    created_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS stage_traces (
    id          BIGSERIAL PRIMARY KEY,
    turn_id     TEXT NOT NULL REFERENCES turns(turn_id),
    stage       TEXT,
    tier        INTEGER,
    model       TEXT,
    seconds     REAL,
    extra_json  JSONB
);

CREATE TABLE IF NOT EXISTS queries (
    id                  BIGSERIAL PRIMARY KEY,
    turn_id             TEXT NOT NULL REFERENCES turns(turn_id),
    query_idx           INTEGER,
    query_type          TEXT,
    label               TEXT,
    code                TEXT,
    result_truncated    BOOLEAN,
    had_error           BOOLEAN
);
"""

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS app_sessions (
    session_id       TEXT PRIMARY KEY,
    started_at       TEXT NOT NULL,
    last_active_at   TEXT,
    timezone         TEXT,
    city             TEXT,
    region           TEXT,
    country          TEXT,
    country_code     TEXT,
    device_type      TEXT,
    os               TEXT,
    browser          TEXT,
    chat_count       INTEGER NOT NULL DEFAULT 0,
    total_turns      INTEGER NOT NULL DEFAULT 0,
    backend_used     TEXT,
    mode_retrieve_count  INTEGER NOT NULL DEFAULT 0,
    mode_explore_count   INTEGER NOT NULL DEFAULT 0,
    mode_reason_count    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_logs (
    chat_id          TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES app_sessions(session_id),
    started_at       TEXT NOT NULL,
    backend          TEXT,
    turn_count       INTEGER NOT NULL DEFAULT 0,
    mode_retrieve_count  INTEGER NOT NULL DEFAULT 0,
    mode_explore_count   INTEGER NOT NULL DEFAULT 0,
    mode_reason_count    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id            TEXT PRIMARY KEY,
    chat_id            TEXT NOT NULL REFERENCES chat_logs(chat_id),
    session_id         TEXT NOT NULL REFERENCES app_sessions(session_id),
    turn_num           INTEGER NOT NULL,
    question_raw       TEXT,
    question_resolved  TEXT,
    mode               INTEGER,
    mode_control       TEXT,
    turn_type          TEXT,
    classification     TEXT,
    total_seconds      REAL,
    turn_status        TEXT,
    error              TEXT,
    narrative_excerpt  TEXT,
    raw_output         TEXT,
    created_at         TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stage_traces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id     TEXT NOT NULL REFERENCES turns(turn_id),
    stage       TEXT,
    tier        INTEGER,
    model       TEXT,
    seconds     REAL,
    extra_json  TEXT
);

CREATE TABLE IF NOT EXISTS queries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id             TEXT NOT NULL REFERENCES turns(turn_id),
    query_idx           INTEGER,
    query_type          TEXT,
    label               TEXT,
    code                TEXT,
    result_truncated    INTEGER,
    had_error           INTEGER
);
"""


# ── Logger class ─────────────────────────────────────────────────────────────

class StreamlitSessionLogger:
    """Structured usage logger for the Streamlit Analytics Agent.

    Thread-safe: all public methods acquire a lock before touching the DB.
    Writes are fire-and-forget — callers should spawn a daemon thread for
    heavy batches (log_stage_traces, log_queries) to avoid blocking the UI.
    """

    def __init__(self, db_url: Optional[str] = None):
        self._lock = threading.Lock()
        self._is_postgres = bool(db_url)
        self._db_url = db_url
        self._conn = None
        self._sqlite_path: Optional[Path] = None

        if self._is_postgres:
            self._connect_postgres()
        else:
            self._sqlite_path = Path(__file__).parent / "logs" / "usage.db"
            self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self._connect_sqlite()

        self._init_schema()

    # ── Connection management ──────────────────────────────────

    def _connect_postgres(self):
        import psycopg2
        self._conn = psycopg2.connect(self._db_url)
        self._conn.autocommit = True

    def _connect_sqlite(self):
        self._conn = sqlite3.connect(
            str(self._sqlite_path), check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.commit()

    def _reconnect(self):
        try:
            if self._is_postgres:
                self._connect_postgres()
            else:
                self._connect_sqlite()
        except Exception:
            pass

    def _execute(self, sql: str, params=()):
        """Execute with automatic reconnect on failure."""
        for attempt in range(2):
            try:
                cur = self._conn.cursor()
                cur.execute(sql, params)
                if not self._is_postgres:
                    self._conn.commit()
                return
            except Exception:
                if attempt == 0:
                    self._reconnect()
                else:
                    raise

    def _init_schema(self):
        ddl = _POSTGRES_DDL if self._is_postgres else _SQLITE_DDL
        with self._lock:
            cur = self._conn.cursor()
            for stmt in ddl.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            if not self._is_postgres:
                self._conn.commit()

    # ── Public: warmup ping ────────────────────────────────────

    def ping(self):
        """Wake the DB (Neon cold-start) and verify connectivity."""
        with self._lock:
            try:
                self._execute("SELECT 1")
            except Exception:
                self._reconnect()
                self._execute("SELECT 1")

    # ── Public: session ────────────────────────────────────────

    def create_session(
        self,
        session_id: str,
        *,
        started_at: Optional[datetime] = None,
        timezone: Optional[str] = None,
        city: Optional[str] = None,
        region: Optional[str] = None,
        country: Optional[str] = None,
        country_code: Optional[str] = None,
        device_type: Optional[str] = None,
        os: Optional[str] = None,
        browser: Optional[str] = None,
    ):
        now = (started_at or datetime.now(tz=timezone_utc())).isoformat()
        ph = "%s" if self._is_postgres else "?"
        sql = f"""
            INSERT INTO app_sessions
              (session_id, started_at, last_active_at, timezone, city, region,
               country, country_code, device_type, os, browser)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
            ON CONFLICT (session_id) DO NOTHING
        """
        with self._lock:
            try:
                self._execute(sql, (
                    session_id, now, now, timezone, city, region,
                    country, country_code, device_type, os, browser,
                ))
            except Exception:
                pass  # Non-fatal: logging must not break the app

    def update_session(
        self,
        session_id: str,
        *,
        turns_delta: int = 0,
        chats_delta: int = 0,
        mode: Optional[int] = None,
        backend: Optional[str] = None,
        last_active_at: Optional[datetime] = None,
    ):
        now = (last_active_at or datetime.now(tz=timezone_utc())).isoformat()
        ph = "%s" if self._is_postgres else "?"
        mode_col = {0: "mode_retrieve_count", 1: "mode_explore_count",
                    2: "mode_reason_count"}.get(mode, None)
        mode_clause = f", {mode_col} = {mode_col} + 1" if mode_col else ""
        # Build params in SQL column order: turns, chats, timestamp, [backend,] session_id
        params: list = [turns_delta, chats_delta, now]
        if backend:
            params.append(backend)
        params.append(session_id)
        sql = f"""
            UPDATE app_sessions
            SET total_turns    = total_turns + {ph},
                chat_count     = chat_count  + {ph},
                last_active_at = {ph}
                {mode_clause}
                {f', backend_used = {ph}' if backend else ''}
            WHERE session_id = {ph}
        """
        with self._lock:
            try:
                self._execute(sql, tuple(params))
            except Exception:
                pass

    # ── Public: chat ───────────────────────────────────────────

    def create_chat(
        self,
        chat_id: str,
        session_id: str,
        *,
        backend: Optional[str] = None,
        started_at: Optional[datetime] = None,
    ):
        now = (started_at or datetime.now(tz=timezone_utc())).isoformat()
        ph = "%s" if self._is_postgres else "?"
        sql = f"""
            INSERT INTO chat_logs (chat_id, session_id, started_at, backend)
            VALUES ({ph},{ph},{ph},{ph})
            ON CONFLICT (chat_id) DO NOTHING
        """
        with self._lock:
            try:
                self._execute(sql, (chat_id, session_id, now, backend))
            except Exception:
                pass

    def update_chat(
        self,
        chat_id: str,
        *,
        turn_count_delta: int = 1,
        mode: Optional[int] = None,
    ):
        ph = "%s" if self._is_postgres else "?"
        mode_col = {0: "mode_retrieve_count", 1: "mode_explore_count",
                    2: "mode_reason_count"}.get(mode, None)
        mode_clause = f", {mode_col} = {mode_col} + 1" if mode_col else ""
        sql = f"""
            UPDATE chat_logs
            SET turn_count = turn_count + {ph} {mode_clause}
            WHERE chat_id  = {ph}
        """
        with self._lock:
            try:
                self._execute(sql, (turn_count_delta, chat_id))
            except Exception:
                pass

    # ── Public: turns ──────────────────────────────────────────

    def log_turn(
        self,
        turn_id: str,
        chat_id: str,
        session_id: str,
        *,
        turn_num: int,
        question_raw: str = "",
        question_resolved: str = "",
        mode: int = 0,
        mode_control: str = "auto",
        turn_type: Optional[str] = None,
        classification: str = "",
        total_seconds: float = 0.0,
        turn_status: str = "done",
        error: Optional[str] = None,
        narrative: str = "",
        raw_output: Optional[dict] = None,
    ):
        excerpt = (narrative or "")[:500]
        raw_json: Optional[str] = None
        if raw_output is not None:
            try:
                # Exclude full query results to keep DB small
                slim = {k: v for k, v in raw_output.items() if k != "queries"}
                raw_json = json.dumps(slim, default=str)
            except Exception:
                pass

        ph = "%s" if self._is_postgres else "?"
        raw_col = raw_json if raw_json else None
        sql = f"""
            INSERT INTO turns
              (turn_id, chat_id, session_id, turn_num,
               question_raw, question_resolved, mode, mode_control,
               turn_type, classification, total_seconds, turn_status,
               error, narrative_excerpt, raw_output)
            VALUES ({(",".join([ph]*15))})
            ON CONFLICT (turn_id) DO NOTHING
        """
        with self._lock:
            try:
                self._execute(sql, (
                    turn_id, chat_id, session_id, turn_num,
                    question_raw, question_resolved, mode, mode_control,
                    turn_type, classification, total_seconds, turn_status,
                    error, excerpt, raw_col,
                ))
            except Exception:
                pass

    # ── Public: stage traces ───────────────────────────────────

    def log_stage_traces(self, turn_id: str, traces: list):
        if not traces:
            return
        ph = "%s" if self._is_postgres else "?"
        sql = f"""
            INSERT INTO stage_traces (turn_id, stage, tier, model, seconds, extra_json)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph})
        """
        with self._lock:
            try:
                cur = self._conn.cursor()
                for rec in traces:
                    extra = {k: v for k, v in rec.items()
                             if k not in ("stage", "tier", "model", "seconds")}
                    extra_str = json.dumps(extra) if extra else None
                    cur.execute(sql, (
                        turn_id,
                        rec.get("stage"),
                        rec.get("tier"),
                        rec.get("model"),
                        rec.get("seconds"),
                        extra_str,
                    ))
                if not self._is_postgres:
                    self._conn.commit()
            except Exception:
                pass

    # ── Public: queries ────────────────────────────────────────

    def log_queries(self, turn_id: str, queries: list):
        if not queries:
            return
        ph = "%s" if self._is_postgres else "?"
        sql = f"""
            INSERT INTO queries
              (turn_id, query_idx, query_type, label, code,
               result_truncated, had_error)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})
        """
        with self._lock:
            try:
                cur = self._conn.cursor()
                for i, q in enumerate(queries):
                    result_str = str(q.get("result") or "")
                    truncated = len(result_str) > 50_000
                    had_err = bool(q.get("error"))
                    cur.execute(sql, (
                        turn_id, i,
                        q.get("type", "primary"),
                        q.get("label", ""),
                        q.get("code", ""),
                        truncated,
                        had_err,
                    ))
                if not self._is_postgres:
                    self._conn.commit()
            except Exception:
                pass


# ── Utility ──────────────────────────────────────────────────────────────────

def timezone_utc():
    return timezone.utc

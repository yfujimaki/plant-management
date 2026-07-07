import sqlite3
import hashlib
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, 'data', 'plants.db')
os.makedirs(os.path.join(BASE_DIR, 'data'), exist_ok=True)


def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


conn = sqlite3.connect(DB)
c = conn.cursor()

c.executescript('''
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password     TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'member',
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS plants (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    location              TEXT,
    photo                 TEXT,
    notes                 TEXT,
    water_days_summer     INTEGER NOT NULL DEFAULT 7,
    water_days_winter     INTEGER NOT NULL DEFAULT 14,
    fertilize_days        INTEGER NOT NULL DEFAULT 60,
    fertilize_note        TEXT,
    soil_days             INTEGER NOT NULL DEFAULT 365,
    floor_x               REAL,
    floor_y               REAL,
    created_by            INTEGER NOT NULL,
    created_at            TEXT NOT NULL,
    archived              INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS care_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id    INTEGER NOT NULL,
    action      TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    logged_at   TEXT NOT NULL,
    comment     TEXT,
    FOREIGN KEY (plant_id) REFERENCES plants(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
''')

c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('season', 'summer')")

# active カラムが未存在の場合に追加（既存DB対応）
try:
    c.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
except Exception:
    pass

# floor_x/floor_y カラムが未存在の場合に追加（既存DB対応）
try:
    c.execute("ALTER TABLE plants ADD COLUMN floor_x REAL")
except Exception:
    pass
try:
    c.execute("ALTER TABLE plants ADD COLUMN floor_y REAL")
except Exception:
    pass

users = [
    # username, display_name, password, role
    ('admin', '管理者', hash_pw('admin123'), 'admin'),
]

for u in users:
    try:
        c.execute('INSERT INTO users (username, display_name, password, role) VALUES (?,?,?,?)', u)
    except sqlite3.IntegrityError:
        pass

conn.commit()
conn.close()
print('OK: DB initialized')

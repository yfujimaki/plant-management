from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3
import base64
import hashlib
import io
import os
import secrets
import uuid
import qrcode
from datetime import date, timedelta
from functools import wraps
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, 'data', 'plants.db')
UPLOAD_DIR = os.path.join(BASE_DIR, 'static', 'uploads')
ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
PORT = 5002
DEFAULT_LOGIN_URL = 'https://fujimaki.pythonanywhere.com/login'


def get_or_create_secret_key():
    key_path = os.path.join(BASE_DIR, 'data', 'secret_key.txt')
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    if os.path.exists(key_path):
        with open(key_path, 'r') as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(key_path, 'w') as f:
        f.write(key)
    return key


app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or get_or_create_secret_key()


ROLES = {
    'member': 'メンバー',
    'admin':  '管理者',
}

DEFAULT_USER_COLOR = '#6c757d'


def contrast_text_color(hex_color):
    hex_color = (hex_color or '').lstrip('#')
    if len(hex_color) != 6:
        return '#ffffff'
    try:
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    except ValueError:
        return '#ffffff'
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return '#000000' if luminance > 0.6 else '#ffffff'

ACTIONS = {
    'water':     {'label': '水やり',   'icon': 'bi-droplet-fill',  'soon_days': 1},
    'fertilize': {'label': '肥料',     'icon': 'bi-flower1',       'soon_days': 7},
    'soil':      {'label': '土の入れ替え', 'icon': 'bi-arrow-repeat', 'soon_days': 14},
}

STATUS_INFO = {
    'overdue': ('要対応', 'danger'),
    'soon':    ('そろそろ', 'warning'),
    'ok':      ('OK', 'success'),
}


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


SUMMER_MONTHS = {4, 5, 6, 7, 8, 9, 10}


def get_season(reference_date=None):
    d = reference_date or date.today()
    return 'summer' if d.month in SUMMER_MONTHS else 'winter'


def water_interval(plant, season):
    return plant['water_days_summer'] if season == 'summer' else plant['water_days_winter']


def plant_status(db, plant, season, today):
    logs = db.execute('''
        SELECT cl.*, u.display_name, u.color FROM care_logs cl
        JOIN users u ON cl.user_id = u.id
        WHERE cl.plant_id = ? ORDER BY cl.logged_at DESC, cl.id DESC
    ''', (plant['id'],)).fetchall()

    result = {}
    for action, meta in ACTIONS.items():
        interval = water_interval(plant, season) if action == 'water' else plant[f'{action}_days']
        last = next((l for l in logs if l['action'] == action), None)
        if last:
            last_date = date.fromisoformat(last['logged_at'])
            last_by = last['display_name']
            last_color = last['color']
        else:
            last_date = date.fromisoformat(plant['created_at'][:10])
            last_by = None
            last_color = None
        next_due = last_date + timedelta(days=interval)
        days_left = (next_due - today).days
        if days_left < 0:
            status = 'overdue'
        elif days_left <= meta['soon_days']:
            status = 'soon'
        else:
            status = 'ok'
        result[action] = {
            'interval': interval,
            'last_date': last_date if last else None,
            'last_by': last_by,
            'last_color': last_color,
            'last_id': last['id'] if last else None,
            'next_due': next_due,
            'days_left': days_left,
            'status': status,
            'done_today': last is not None and last_date == today,
        }
    return result


def parse_log_date(value):
    if not value:
        return date.today(), None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return date.today(), '日付の形式が正しくないため、本日の日付を使用しました。'
    if d > date.today():
        return date.today(), '未来の日付は指定できないため、本日の日付を使用しました。'
    return d, None


def overall_status(status_map):
    order = {'overdue': 0, 'soon': 1, 'ok': 2}
    worst = min(status_map.values(), key=lambda s: order[s['status']])
    return worst['status']


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('ログインが必要です。', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('管理者のみ実行できます。', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


@app.context_processor
def inject_globals():
    db = get_db()
    season = get_season()
    return {
        'ROLES': ROLES, 'ACTIONS': ACTIONS, 'STATUS_INFO': STATUS_INFO, 'current_season': season,
        'contrast_text_color': contrast_text_color, 'DEFAULT_USER_COLOR': DEFAULT_USER_COLOR,
    }


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    db = get_db()
    users = db.execute(
        'SELECT username, display_name FROM users WHERE active=1 ORDER BY display_name'
    ).fetchall()
    if request.method == 'POST':
        user = db.execute(
            'SELECT * FROM users WHERE username=? AND password=?',
            (request.form['username'], hash_pw(request.form['password']))
        ).fetchone()
        if user and not user['active']:
            flash('このアカウントは無効化されています。管理者にご確認ください。', 'danger')
        elif user:
            session.update({
                'user_id':      user['id'],
                'username':     user['username'],
                'display_name': user['display_name'],
                'role':         user['role'],
            })
            return redirect(url_for('dashboard'))
        else:
            flash('登録者またはパスワードが間違っています。', 'danger')
    return render_template('login.html', users=users)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    db = get_db()
    season = get_season()
    today = date.today()
    plants = db.execute('SELECT * FROM plants WHERE archived=0 ORDER BY name').fetchall()

    todo = []
    for plant in plants:
        statuses = plant_status(db, plant, season, today)
        for action, s in statuses.items():
            if s['status'] in ('overdue', 'soon') and not s['done_today']:
                todo.append({'plant': plant, 'action': action, **s})
    todo.sort(key=lambda x: x['days_left'])

    return render_template('dashboard.html', todo=todo, plant_count=len(plants), today=today)


# ── Plant List ────────────────────────────────────────────────────────────────

@app.route('/plants')
@login_required
def plant_list():
    db = get_db()
    season = get_season()
    today = date.today()
    show_archived = request.args.get('archived') == '1'
    plants = db.execute(
        'SELECT * FROM plants WHERE archived=? ORDER BY name',
        (1 if show_archived else 0,)
    ).fetchall()

    cards = []
    for plant in plants:
        statuses = plant_status(db, plant, season, today)
        cards.append({'plant': plant, 'statuses': statuses, 'overall': overall_status(statuses)})

    return render_template('plant_list.html', cards=cards, show_archived=show_archived)


# ── Plant New / Edit ──────────────────────────────────────────────────────────

def save_uploaded_image(prefix, file):
    if not file or file.filename == '':
        return None
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_EXT:
        flash('画像はpng/jpg/jpeg/gif/webp形式のみ対応しています。', 'warning')
        return None
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    filename = f'{prefix}_{uuid.uuid4().hex[:8]}.{ext}'
    file.save(os.path.join(UPLOAD_DIR, secure_filename(filename)))
    return filename


def save_photo(plant_id, file):
    return save_uploaded_image(f'plant_{plant_id}', file)


@app.route('/plants/new', methods=['GET', 'POST'])
@login_required
def plant_new():
    if request.method == 'POST':
        f = request.form
        db = get_db()
        try:
            db.execute('''
                INSERT INTO plants (
                    name, location, notes,
                    water_days_summer, water_days_winter,
                    fertilize_days, fertilize_note, soil_days,
                    created_by, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ''', (
                f['name'], f.get('location', ''), f.get('notes', ''),
                int(f.get('water_days_summer') or 7),
                int(f.get('water_days_winter') or 14),
                int(f.get('fertilize_days') or 60),
                f.get('fertilize_note', ''),
                int(f.get('soil_days') or 365),
                session['user_id'], date.today().isoformat(),
            ))
            plant_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            photo = save_photo(plant_id, request.files.get('photo'))
            if photo:
                db.execute('UPDATE plants SET photo=? WHERE id=?', (photo, plant_id))
            db.commit()
            flash(f'「{f["name"]}」を登録しました！', 'success')
            return redirect(url_for('plant_detail', plant_id=plant_id))
        except Exception as e:
            db.rollback()
            flash(f'登録に失敗しました: {e}', 'danger')

    return render_template('plant_form.html', plant=None)


def parse_bulk_plant_text(text):
    lines = [l.strip() for l in text.replace('\r\n', '\n').split('\n') if l.strip()]
    plants = []
    current = None
    for line in lines:
        if line.startswith('【'):
            if current is not None:
                current['notes'].append(line)
        else:
            if current is not None:
                plants.append(current)
            current = {'name': line, 'notes': []}
    if current is not None:
        plants.append(current)
    return [{'name': p['name'], 'notes': '\n'.join(p['notes'])} for p in plants if p['name']]


@app.route('/plants/bulk_import', methods=['GET', 'POST'])
@login_required
def plant_bulk_import():
    if request.method == 'POST':
        parsed = parse_bulk_plant_text(request.form.get('bulk_text', ''))
        db = get_db()
        created = []
        for p in parsed:
            db.execute('''
                INSERT INTO plants (
                    name, location, notes,
                    water_days_summer, water_days_winter,
                    fertilize_days, fertilize_note, soil_days,
                    created_by, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ''', (
                p['name'], '', p['notes'],
                7, 14, 60, '', 365,
                session['user_id'], date.today().isoformat(),
            ))
            created.append(p['name'])
        db.commit()
        if created:
            flash(f'{len(created)}件の植物を登録しました！配置場所や水やり頻度は各植物の編集画面から調整してください。（' + '、'.join(created) + '）', 'success')
        else:
            flash('登録できるデータが見つかりませんでした。貼り付け内容の形式をご確認ください。', 'warning')
        return redirect(url_for('plant_list'))

    return render_template('plant_bulk_import.html')


@app.route('/plants/<int:plant_id>/edit', methods=['GET', 'POST'])
@login_required
def plant_edit(plant_id):
    db = get_db()
    plant = db.execute('SELECT * FROM plants WHERE id=?', (plant_id,)).fetchone()
    if not plant:
        flash('植物が見つかりません。', 'warning')
        return redirect(url_for('plant_list'))

    if request.method == 'POST':
        f = request.form
        try:
            db.execute('''
                UPDATE plants SET name=?, location=?, notes=?,
                water_days_summer=?, water_days_winter=?,
                fertilize_days=?, fertilize_note=?, soil_days=?
                WHERE id=?
            ''', (
                f['name'], f.get('location', ''), f.get('notes', ''),
                int(f.get('water_days_summer') or 7),
                int(f.get('water_days_winter') or 14),
                int(f.get('fertilize_days') or 60),
                f.get('fertilize_note', ''),
                int(f.get('soil_days') or 365),
                plant_id,
            ))
            photo = save_photo(plant_id, request.files.get('photo'))
            if photo:
                db.execute('UPDATE plants SET photo=? WHERE id=?', (photo, plant_id))
            db.commit()
            flash('植物情報を更新しました。', 'success')
            return redirect(url_for('plant_detail', plant_id=plant_id))
        except Exception as e:
            db.rollback()
            flash(f'更新に失敗しました: {e}', 'danger')

    return render_template('plant_form.html', plant=plant)


@app.route('/plants/<int:plant_id>/archive', methods=['POST'])
@login_required
@admin_required
def plant_archive(plant_id):
    db = get_db()
    archived = 0 if request.form.get('unarchive') else 1
    db.execute('UPDATE plants SET archived=? WHERE id=?', (archived, plant_id))
    db.commit()
    flash('アーカイブ状態を更新しました。' if archived else '一覧に復帰させました。', 'success')
    return redirect(url_for('plant_list'))


# ── Plant Detail ──────────────────────────────────────────────────────────────

@app.route('/plants/<int:plant_id>')
@login_required
def plant_detail(plant_id):
    db = get_db()
    plant = db.execute('SELECT * FROM plants WHERE id=?', (plant_id,)).fetchone()
    if not plant:
        flash('植物が見つかりません。', 'warning')
        return redirect(url_for('plant_list'))

    season = get_season()
    today = date.today()
    statuses = plant_status(db, plant, season, today)

    history = db.execute('''
        SELECT cl.*, u.display_name, u.color FROM care_logs cl
        JOIN users u ON cl.user_id = u.id
        WHERE cl.plant_id = ? ORDER BY cl.logged_at DESC, cl.id DESC LIMIT 30
    ''', (plant_id,)).fetchall()

    return render_template('plant_detail.html', plant=plant, statuses=statuses, history=history, today=today)


@app.route('/plants/<int:plant_id>/log', methods=['POST'])
@login_required
def plant_log(plant_id):
    db = get_db()
    plant = db.execute('SELECT * FROM plants WHERE id=?', (plant_id,)).fetchone()
    if not plant:
        flash('植物が見つかりません。', 'warning')
        return redirect(url_for('plant_list'))

    action = request.form.get('action')
    if action not in ACTIONS:
        flash('不正な操作です。', 'danger')
        return redirect(url_for('plant_detail', plant_id=plant_id))

    log_date, date_warning = parse_log_date(request.form.get('log_date'))
    if date_warning:
        flash(date_warning, 'warning')
    log_date_str = log_date.isoformat()

    already = db.execute(
        "SELECT 1 FROM care_logs WHERE plant_id=? AND action=? AND logged_at=?",
        (plant_id, action, log_date_str)
    ).fetchone()
    if already:
        flash(f'「{ACTIONS[action]["label"]}」は{log_date_str}分すでに記録済みです。', 'warning')
        return redirect(url_for('plant_detail', plant_id=plant_id))

    db.execute(
        'INSERT INTO care_logs (plant_id, action, user_id, logged_at, comment) VALUES (?,?,?,?,?)',
        (plant_id, action, session['user_id'], log_date_str, request.form.get('comment', ''))
    )
    db.commit()
    if log_date == date.today():
        flash(f'{ACTIONS[action]["label"]}を記録しました！', 'success')
    else:
        flash(f'{ACTIONS[action]["label"]}を{log_date_str}分として記録しました！', 'success')
    return redirect(url_for('plant_detail', plant_id=plant_id))


@app.route('/plants/<int:plant_id>/log/<int:log_id>/delete', methods=['POST'])
@login_required
def plant_log_delete(plant_id, log_id):
    db = get_db()
    log = db.execute('SELECT * FROM care_logs WHERE id=? AND plant_id=?', (log_id, plant_id)).fetchone()
    if not log:
        flash('記録が見つかりません。', 'warning')
        return redirect(url_for('plant_detail', plant_id=plant_id))

    db.execute('DELETE FROM care_logs WHERE id=?', (log_id,))
    db.commit()
    flash(f'{ACTIONS[log["action"]]["label"]}の記録を取り消しました。', 'success')
    return redirect(url_for('plant_detail', plant_id=plant_id))


# ── Bulk Watering ─────────────────────────────────────────────────────────────

@app.route('/water_all', methods=['GET', 'POST'])
@login_required
def water_all():
    db = get_db()
    plants = db.execute('SELECT * FROM plants WHERE archived=0 ORDER BY id').fetchall()
    today = date.today()
    today_str = today.isoformat()

    if request.method == 'POST':
        log_date, date_warning = parse_log_date(request.form.get('log_date'))
        if date_warning:
            flash(date_warning, 'warning')
        log_date_str = log_date.isoformat()

        selected_ids = set(request.form.getlist('plant_ids'))
        watered, skipped = [], []
        for plant in plants:
            if str(plant['id']) not in selected_ids:
                continue
            already = db.execute(
                "SELECT 1 FROM care_logs WHERE plant_id=? AND action='water' AND logged_at=?",
                (plant['id'], log_date_str)
            ).fetchone()
            if already:
                skipped.append(plant['name'])
                continue
            db.execute(
                'INSERT INTO care_logs (plant_id, action, user_id, logged_at, comment) VALUES (?,?,?,?,?)',
                (plant['id'], 'water', session['user_id'], log_date_str, '')
            )
            watered.append(plant['name'])
        db.commit()

        date_note = '' if log_date == today else f'（{log_date_str}分として）'
        if watered:
            flash(f'{len(watered)}件の水やりを記録しました{date_note}！（' + '、'.join(watered) + '）', 'success')
        if skipped:
            flash(f'{len(skipped)}件は{log_date_str}分ですでに記録済みのためスキップしました。（' + '、'.join(skipped) + '）', 'warning')
        if not watered and not skipped:
            flash('選択された植物がありませんでした。', 'warning')
        return redirect(url_for('water_all'))

    rows = []
    for plant in plants:
        already = db.execute(
            "SELECT 1 FROM care_logs WHERE plant_id=? AND action='water' AND logged_at=?",
            (plant['id'], today_str)
        ).fetchone()
        rows.append({'plant': plant, 'done_today': bool(already)})

    return render_template('water_all.html', rows=rows, today=today_str)


@app.route('/bulk_undo', methods=['GET', 'POST'])
@login_required
def bulk_undo():
    db = get_db()

    if request.method == 'POST':
        target_date, _ = parse_log_date(request.form.get('log_date'))
        target_date_str = target_date.isoformat()
        log_ids = request.form.getlist('log_ids')
        if log_ids:
            db.executemany('DELETE FROM care_logs WHERE id=?', [(i,) for i in log_ids])
            db.commit()
            flash(f'{len(log_ids)}件の記録を取り消しました。', 'success')
        else:
            flash('選択された記録がありませんでした。', 'warning')
        return redirect(url_for('bulk_undo', date=target_date_str))

    target_date, _ = parse_log_date(request.args.get('date'))
    target_date_str = target_date.isoformat()

    logs = db.execute('''
        SELECT cl.id, cl.action, cl.plant_id, p.name AS plant_name, u.display_name, u.color
        FROM care_logs cl
        JOIN plants p ON cl.plant_id = p.id
        JOIN users u ON cl.user_id = u.id
        WHERE cl.logged_at = ?
        ORDER BY p.name, cl.action
    ''', (target_date_str,)).fetchall()

    return render_template('bulk_undo.html', logs=logs, target_date=target_date_str,
                            today=date.today().isoformat())


# ── Floor Plan ────────────────────────────────────────────────────────────────

def get_setting(db, key):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else None


def set_setting(db, key, value):
    if db.execute('SELECT 1 FROM settings WHERE key=?', (key,)).fetchone():
        db.execute('UPDATE settings SET value=? WHERE key=?', (value, key))
    else:
        db.execute('INSERT INTO settings (key, value) VALUES (?,?)', (key, value))


@app.route('/floorplan')
@login_required
def floorplan():
    db = get_db()
    season = get_season()
    today = date.today()
    image = get_setting(db, 'floorplan_image')
    plants = db.execute('SELECT * FROM plants WHERE archived=0 ORDER BY name').fetchall()
    by_registration = db.execute('SELECT id FROM plants WHERE archived=0 ORDER BY id').fetchall()
    numbers = {row['id']: i + 1 for i, row in enumerate(by_registration)}

    markers = []
    unplaced = []
    for plant in plants:
        if plant['floor_x'] is None or plant['floor_y'] is None:
            unplaced.append(plant)
        else:
            statuses = plant_status(db, plant, season, today)
            markers.append({'plant': plant, 'overall': overall_status(statuses), 'no': numbers[plant['id']]})

    return render_template('floorplan.html', floorplan_image=image, markers=markers,
                            unplaced=unplaced, numbers=numbers)


@app.route('/floorplan/upload', methods=['POST'])
@login_required
@admin_required
def floorplan_upload():
    filename = save_uploaded_image('floorplan', request.files.get('floorplan'))
    if filename:
        db = get_db()
        set_setting(db, 'floorplan_image', filename)
        db.commit()
        flash('フロア配置図を更新しました。', 'success')
    return redirect(url_for('floorplan'))


@app.route('/floorplan/place', methods=['POST'])
@login_required
def floorplan_place():
    data = request.get_json(silent=True) or request.form
    try:
        plant_id = int(data.get('plant_id'))
        x = max(0.0, min(100.0, float(data.get('x'))))
        y = max(0.0, min(100.0, float(data.get('y'))))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'invalid parameters'}), 400

    db = get_db()
    db.execute('UPDATE plants SET floor_x=?, floor_y=? WHERE id=?', (x, y, plant_id))
    db.commit()
    return jsonify({'ok': True, 'x': x, 'y': y})


@app.route('/floorplan/unplace', methods=['POST'])
@login_required
def floorplan_unplace():
    data = request.get_json(silent=True) or request.form
    plant_id = data.get('plant_id')
    db = get_db()
    db.execute('UPDATE plants SET floor_x=NULL, floor_y=NULL WHERE id=?', (plant_id,))
    db.commit()
    return jsonify({'ok': True})


# ── History Grid ──────────────────────────────────────────────────────────────

WEEKDAY_JA = ['月', '火', '水', '木', '金', '土', '日']


@app.route('/history')
@login_required
def history():
    db = get_db()
    try:
        days = int(request.args.get('days', 30))
    except ValueError:
        days = 30
    days = max(7, min(days, 90))

    today = date.today()
    start = today - timedelta(days=days - 1)
    date_list = [start + timedelta(days=i) for i in range(days)]

    plants = db.execute('SELECT * FROM plants WHERE archived=0 ORDER BY name').fetchall()
    by_registration = db.execute('SELECT id FROM plants WHERE archived=0 ORDER BY id').fetchall()
    numbers = {row['id']: i + 1 for i, row in enumerate(by_registration)}

    logs = db.execute('''
        SELECT cl.id, cl.plant_id, cl.action, cl.logged_at, u.display_name, u.color
        FROM care_logs cl JOIN users u ON cl.user_id = u.id
        WHERE cl.logged_at >= ?
    ''', (start.isoformat(),)).fetchall()

    grid = {}
    for l in logs:
        key = (l['plant_id'], l['logged_at'])
        grid.setdefault(key, []).append({
            'id': l['id'], 'action': l['action'], 'by': l['display_name'], 'color': l['color'],
        })

    rows = []
    for p in plants:
        cells = [grid.get((p['id'], d.isoformat()), []) for d in date_list]
        rows.append({'plant': p, 'cells': cells, 'no': numbers[p['id']]})

    active_users = db.execute(
        'SELECT display_name, color FROM users WHERE active=1 ORDER BY display_name'
    ).fetchall()

    return render_template('history.html', rows=rows, date_list=date_list, days=days,
                            weekday_ja=WEEKDAY_JA, users=active_users)


# ── QR Login ──────────────────────────────────────────────────────────────────

@app.route('/qr')
@login_required
def qr_code():
    login_url = request.args.get('url') or DEFAULT_LOGIN_URL
    qr_img = qrcode.make(login_url)
    buf = io.BytesIO()
    qr_img.save(buf, format='PNG')
    qr_data = base64.b64encode(buf.getvalue()).decode('ascii')
    return render_template('qr.html', login_url=login_url, qr_data=qr_data)


# ── User Management (Admin) ───────────────────────────────────────────────────

@app.route('/users')
@login_required
@admin_required
def user_list():
    db = get_db()
    users = db.execute('SELECT * FROM users ORDER BY active DESC, role, display_name').fetchall()
    return render_template('users.html', users=users)


@app.route('/users/new', methods=['POST'])
@login_required
@admin_required
def user_new():
    db = get_db()
    f = request.form
    try:
        db.execute('INSERT INTO users (username, display_name, password, role, color) VALUES (?,?,?,?,?)',
                   (f['username'], f['display_name'], hash_pw(f['password']), f.get('role', 'member'),
                    f.get('color') or DEFAULT_USER_COLOR))
        db.commit()
        flash(f'ユーザー「{f["display_name"]}」を作成しました。', 'success')
    except sqlite3.IntegrityError:
        flash('そのユーザー名は既に使用されています。', 'danger')
    return redirect(url_for('user_list'))


@app.route('/users/<int:user_id>/reset', methods=['POST'])
@login_required
@admin_required
def user_reset(user_id):
    db = get_db()
    db.execute('UPDATE users SET password=? WHERE id=?',
               (hash_pw(request.form['password']), user_id))
    db.commit()
    flash('パスワードをリセットしました。', 'success')
    return redirect(url_for('user_list'))


@app.route('/users/<int:user_id>/edit', methods=['POST'])
@login_required
@admin_required
def user_edit(user_id):
    db = get_db()
    f = request.form
    try:
        db.execute('UPDATE users SET username=?, display_name=?, role=?, color=? WHERE id=?',
                   (f['username'], f['display_name'], f.get('role', 'member'),
                    f.get('color') or DEFAULT_USER_COLOR, user_id))
        db.commit()
        flash('ユーザー情報を更新しました。', 'success')
    except sqlite3.IntegrityError:
        flash('そのユーザー名は既に使用されています。', 'danger')
    return redirect(url_for('user_list'))


@app.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def user_delete(user_id):
    if user_id == session['user_id']:
        flash('自分自身は削除・無効化できません。', 'danger')
        return redirect(url_for('user_list'))

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        flash('ユーザーが見つかりません。', 'warning')
        return redirect(url_for('user_list'))

    has_plants = db.execute('SELECT 1 FROM plants WHERE created_by=?', (user_id,)).fetchone()
    has_logs = db.execute('SELECT 1 FROM care_logs WHERE user_id=?', (user_id,)).fetchone()

    if has_plants or has_logs:
        db.execute('UPDATE users SET active=0 WHERE id=?', (user_id,))
        db.commit()
        flash(f'「{user["display_name"]}」には登録・お世話の履歴があるため完全には削除できません。ログインのみ無効化しました。', 'warning')
    else:
        db.execute('DELETE FROM users WHERE id=?', (user_id,))
        db.commit()
        flash(f'「{user["display_name"]}」を削除しました。', 'success')
    return redirect(url_for('user_list'))


@app.route('/users/<int:user_id>/reactivate', methods=['POST'])
@login_required
@admin_required
def user_reactivate(user_id):
    db = get_db()
    db.execute('UPDATE users SET active=1 WHERE id=?', (user_id,))
    db.commit()
    flash('ログインを再度有効にしました。', 'success')
    return redirect(url_for('user_list'))


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=PORT, debug=False)

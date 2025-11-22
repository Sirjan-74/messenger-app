#app.py
import eventlet
eventlet.monkey_patch()

from flask import Flask
from flask_socketio import SocketIO
import os
import secrets
import base64
import mimetypes
import threading
import time
import re
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, send_from_directory, send_file)

from werkzeug.security import generate_password_hash, check_password_hash
import cloudinary
import cloudinary.uploader
import cloudinary.api



cloudinary.config(
  cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME"),
  api_key = os.environ.get("CLOUDINARY_API_KEY"),
  api_secret = os.environ.get("CLOUDINARY_API_SECRET")
)

# Optional PIL import
try:
    from PIL import Image
    PIL_AVAILABLE = True
except Exception:
    Image = None
    PIL_AVAILABLE = False

# MongoDB setup
IS_DB_AVAILABLE = False
try:
    from flask_pymongo import PyMongo
    IS_DB_AVAILABLE = True
except ImportError:
    IS_DB_AVAILABLE = False
    print("ERROR: PyMongo not installed. Run `pip install flask-pymongo` if using DB support")



# Flask app config
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'static/uploads')
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 100 * 1024 * 1024))  # 100MB default
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # disable caching for development


socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet", manage_session=True)

# Allowed extensions
ALLOWED_EXTENSIONS = set(os.environ.get(
    "ALLOWED_EXTENSIONS",
    "txt,pdf,png,jpg,jpeg,gif,mp4,mp3,wav,doc,docx,xls,xlsx,ppt,pptx,zip,rar,webm"
).split(","))

# ensure upload directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'thumbnails'), exist_ok=True)

# Mongo client (only if MONGO_URI set and flask_pymongo available)
mongo = None
if IS_DB_AVAILABLE:
    app.config["MONGO_URI"] = os.environ.get("MONGO_URI")
    if not app.config["MONGO_URI"]:
        app.logger.warning("❌ ERROR: MONGO_URI NOT SET IN ENV — DB disabled")
        IS_DB_AVAILABLE = False
    else:
        try:
            mongo = PyMongo(app)
            mongo.db.command("ping")
            app.logger.info("✅ Connected to MongoDB Atlas")
            # create indexes
            try:
                mongo.db.messages.create_index([("room", 1), ("timestamp", -1)])
                mongo.db.users.create_index([("email", 1)], unique=True)
                mongo.db.friends.create_index([("user1", 1), ("user2", 1)])
            except Exception:
                pass
        except Exception as e:
            app.logger.warning(f"❌ MongoDB Error: {e}")
            IS_DB_AVAILABLE = False
else:
    app.logger.info("Database support disabled (flask_pymongo not installed).")

# In-memory state
# active_users: sid -> {'email':..., 'username':..., 'connected_at':...}
active_users = {}
user_rooms = {}
typing_users = {}

# ---------------------------
# Helper utilities
# ---------------------------
def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if 'user' not in session:
            flash('Please log in first.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapped

def get_file_type(filename):
    if not filename or '.' not in filename:
        return 'file'
    ext = filename.rsplit('.', 1)[1].lower()
    image_types = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg']
    video_types = ['mp4', 'avi', 'mov', 'wmv', 'mkv', 'webm', 'flv']
    audio_types = ['mp3', 'wav', 'ogg', 'm4a', 'aac', 'flac']
    document_types = ['pdf', 'doc', 'docx', 'txt', 'rtf', 'odt', 'xlsx', 'pptx']
    if ext in image_types:
        return 'image'
    if ext in video_types:
        return 'video'
    if ext in audio_types:
        return 'audio'
    if ext in document_types:
        return 'document'
    return 'file'

def allowed_file(filename):
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS

def create_thumbnail(file_path, filename):
    if not PIL_AVAILABLE:
        app.logger.debug("Pillow not available — skipping thumbnail creation")
        return None
    try:
        with Image.open(file_path) as img:
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = background
            try:
                resample = Image.Resampling.LANCZOS
            except Exception:
                resample = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.ANTIALIAS
            img.thumbnail((200, 200), resample)
            thumbnail_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'thumbnails')
            os.makedirs(thumbnail_dir, exist_ok=True)
            thumbnail_filename = f"thumb_{filename}"
            thumbnail_path = os.path.join(thumbnail_dir, thumbnail_filename)
            img.save(thumbnail_path, 'JPEG', quality=85)
            return f"/uploads/thumbnails/{thumbnail_filename}"
    except Exception as e:
        app.logger.debug(f"Thumbnail creation failed for {filename}: {e}")
        return None

# ---------------------------
# Room/username helpers
# ---------------------------
def normalize_username_for_room(username: str) -> str:
    if not username:
        return username
    uname = username.strip()
    uname = re.sub(r'\s+', '-', uname)
    uname = re.sub(r'[^A-Za-z0-9_\-\.]', '', uname)
    return uname

def get_username_from_email(email: str):
    if not email:
        return email
    if IS_DB_AVAILABLE:
        try:
            user = mongo.db.users.find_one({'email': email})
            if user and user.get('username'):
                return user['username']
        except Exception:
            pass
    prefix = email.split('@', 1)[0]
    return prefix.capitalize()

def get_private_room_id(user_a_identifier, user_b_identifier):
    def _normalize(idf):
        if not idf:
            return ''
        if '@' in idf:
            uname = get_username_from_email(idf)
        else:
            uname = idf
        return normalize_username_for_room(uname)

    a = _normalize(user_a_identifier)
    b = _normalize(user_b_identifier)
    pair = sorted([a, b], key=lambda x: x.lower())
    return f"private_{pair[0]}-{pair[1]}"

def convert_email_room_to_username_room_if_needed(room):
    if not room:
        return room, False
    if '@' not in room:
        return room, False

    base = room
    if base.startswith('private_'):
        base = base[len('private_'):]
    parts = base.split('-')
    if len(parts) >= 2:
        for i in range(1, len(parts)):
            left = '-'.join(parts[:i])
            right = '-'.join(parts[i:])
            if '@' in left and '@' in right:
                email1 = left
                email2 = right
                break
        else:
            email1 = parts[0]
            email2 = '-'.join(parts[1:])
    else:
        return room, False

    uname1 = get_username_from_email(email1)
    uname2 = get_username_from_email(email2)

    new_room = get_private_room_id(uname1, uname2)

    perform_migration = os.environ.get("PERFORM_ROOM_MIGRATION", "") == "1"
    if IS_DB_AVAILABLE and perform_migration:
        try:
            mongo.db.friends.update_many({'room': room}, {'$set': {'room': new_room}})
            mongo.db.messages.update_many({'room': room}, {'$set': {'room': new_room}})
            app.logger.info(f"✅ Migrated room {room} -> {new_room} in DB")
            return new_room, True
        except Exception as e:
            app.logger.warning(f"Could not migrate room in DB: {e}")
            return new_room, False

    return new_room, False

# ---------------------------
# DB helper functions (only used when DB enabled)
# ---------------------------
def get_unread_count(user_email, room):
    if not IS_DB_AVAILABLE:
        return 0
    user_room_data = mongo.db.user_rooms.find_one({'user_email': user_email, 'room': room})
    last_read = user_room_data.get('last_read') if user_room_data else datetime.min.replace(tzinfo=timezone.utc)
    unread_count = mongo.db.messages.count_documents({
        'room': room,
        'timestamp': {'$gt': last_read},
        'author_email': {'$ne': user_email}
    })
    return unread_count

def mark_messages_as_read(user_email, room):
    if not IS_DB_AVAILABLE:
        return
    mongo.db.user_rooms.update_one(
        {'user_email': user_email, 'room': room},
        {'$set': {'last_read': datetime.now(timezone.utc), 'user_email': user_email, 'room': room}},
        upsert=True
    )

def get_friend_requests(email):
    if not IS_DB_AVAILABLE:
        return []
    requests = []
    for fr in mongo.db.friend_requests.find({'recipient': email, 'status': 'pending'}):
        sender = mongo.db.users.find_one({'email': fr['sender']})
        if sender:
            requests.append({'sender': fr['sender'], 'sender_username': sender['username'], 'sent_at': fr.get('sent_at', datetime.utcnow())})
    return requests

def get_user_friends(email):
    if not IS_DB_AVAILABLE:
        return []
    friends = []
    for rel in mongo.db.friends.find({'$or': [{'user1': email}, {'user2': email}]}):
        friend_email = rel['user2'] if rel['user1'] == email else rel['user1']
        fuser = mongo.db.users.find_one({'email': friend_email})
        if not fuser:
            continue
        is_online = any(u['email'] == friend_email for u in active_users.values())
        stored_room = rel.get('room')
        if stored_room and '@' in stored_room:
            try:
                new_room, migrated = convert_email_room_to_username_room_if_needed(stored_room)
                stored_room = new_room
            except Exception:
                stored_room = rel.get('room')
        last_message = mongo.db.messages.find_one({'room': stored_room}, sort=[('timestamp', -1)]) if stored_room else None
        friends.append({
            'username': fuser['username'],
            'email': friend_email,
            'avatar': fuser.get('avatar', f'https://ui-avatars.com/api/?name={fuser["username"]}&background=random&color=fff'),
            'is_online': is_online,
            'room': stored_room,
            'last_message': last_message.get('text', '') if last_message else '',
            'last_message_time': last_message.get('timestamp') if last_message else None,
            'unread_count': get_unread_count(email, stored_room) if stored_room else 0
        })
    friends.sort(key=lambda f: (not f['is_online'], f['last_message_time'] or datetime.min), reverse=True)
    return friends

def get_all_users_for_discovery(current_user_email):
    if not IS_DB_AVAILABLE:
        return []
    friend_emails = set()
    for rel in mongo.db.friends.find({'$or': [{'user1': current_user_email}, {'user2': current_user_email}]}):
        friend_email = rel['user2'] if rel['user1'] == current_user_email else rel['user1']
        friend_emails.add(friend_email)
    pending_requests = set()
    for req in mongo.db.friend_requests.find({'$or': [{'sender': current_user_email, 'status': 'pending'}, {'recipient': current_user_email, 'status': 'pending'}]}):
        pending_requests.add(req['sender'] if req['sender'] != current_user_email else req['recipient'])
    all_users = []
    users_cursor = mongo.db.users.find({'email': {'$ne': current_user_email}}).sort('username', 1)
    for user in users_cursor:
        is_online = any(u['email'] == user['email'] for u in active_users.values())
        if user['email'] in friend_emails:
            status = 'friends'
        elif user['email'] in pending_requests:
            status = 'pending'
        else:
            status = 'none'
        if status != 'friends':
            all_users.append({
                'username': user['username'],
                'email': user['email'],
                'avatar': user.get('avatar', f'https://ui-avatars.com/api/?name={user["username"]}&background=random&color=fff'),
                'is_online': is_online,
                'status': status,
                'bio': user.get('bio', ''),
                'joined_date': user.get('created_at', datetime.now(timezone.utc))
            })
    return all_users

def get_user_stats(user_email):
    if not IS_DB_AVAILABLE:
        return {'friends_count': 0, 'messages_sent': 0, 'messages_received': 0, 'total_conversations': 0, 'join_date': datetime.now(timezone.utc)}
    friends_count = mongo.db.friends.count_documents({'$or': [{'user1': user_email}, {'user2': user_email}]})
    messages_sent = mongo.db.messages.count_documents({'author_email': user_email})
    user_rooms_set = set()
    for rel in mongo.db.friends.find({'$or': [{'user1': user_email}, {'user2': user_email}]}):
        if rel.get('room'):
            room_val = rel.get('room')
            if '@' in room_val:
                room_val, _ = convert_email_room_to_username_room_if_needed(room_val)
            user_rooms_set.add(room_val)
    total_messages_in_conversations = mongo.db.messages.count_documents({'room': {'$in': list(user_rooms_set)}}) if user_rooms_set else 0
    messages_received = total_messages_in_conversations - messages_sent
    user_data = mongo.db.users.find_one({'email': user_email})
    join_date = user_data.get('created_at', datetime.now(timezone.utc)) if user_data else datetime.now(timezone.utc)
    return {'friends_count': friends_count, 'messages_sent': messages_sent, 'messages_received': max(0, messages_received), 'total_conversations': len(user_rooms_set), 'join_date': join_date}

# ---------------------------
# Participants / unread helper utilities
# ---------------------------
def get_sids_for_email(email):
    """Return list of active socket sids for the given email."""
    return [sid for sid, info in active_users.items() if info.get('email') == email]

def get_emails_for_room(room):
    """Return the participant emails for a room using friends collection or fallbacks."""
    if not IS_DB_AVAILABLE:
        return []
    try:
        rel = mongo.db.friends.find_one({'room': room})
        if rel and 'user1' in rel and 'user2' in rel:
            return [rel['user1'], rel['user2']]
    except Exception:
        pass
    # fallback: try to parse private_ format
    if room.startswith('private_'):
        base = room[len('private_'):]
        parts = base.split('-')
        if len(parts) >= 2:
            left = parts[0]
            right = '-'.join(parts[1:])
            # try direct username lookup
            u1 = mongo.db.users.find_one({'username': left})
            u2 = mongo.db.users.find_one({'username': right})
            emails = []
            if u1:
                emails.append(u1['email'])
            if u2:
                emails.append(u2['email'])
            return emails
    return []

def update_unread_for_room(room, message_timestamp, author_email):
    """Fix: Correct unread logic + skip sender + live emit."""
    if not IS_DB_AVAILABLE:
        return
    try:
        participants = get_emails_for_room(room)

        for participant in participants:
            if participant == author_email:
                continue

            unread_count = get_unread_count(participant, room)
            sockets = get_sids_for_email(participant)

            for sid in sockets:
                emit('unread_update', {
                    'room': room,
                    'unread_count': unread_count
                }, to=sid)

    except Exception as e:
        app.logger.error(f"Unread update error: {e}")


# ---------------------------
# Routes: Auth, Dashboard, Chat
# ---------------------------
@app.route('/')
def home():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').lower().strip()
        password = request.form.get('password', '')
        if not IS_DB_AVAILABLE:
            flash('Database connection not available.', 'error')
            return render_template('login.html')
        user = mongo.db.users.find_one({'email': email})
        if user and check_password_hash(user['password'], password):
            session['user'] = {
                'email': user['email'],
                'username': user['username'],
                'avatar': user.get('avatar', f'https://ui-avatars.com/api/?name={user["username"]}&background=random&color=fff')
            }
            session.permanent = True
            mongo.db.users.update_one({'email': email}, {'$set': {'last_login': datetime.now(timezone.utc)}})
            flash(f'Welcome back, {user["username"]}!', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid email or password.', 'error')
    return render_template('login.html')

@app.route('/register', methods=['GET','POST'])
def register():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        if not IS_DB_AVAILABLE:
            flash('Database connection not available.', 'error')
            return render_template('register.html')
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').lower().strip()
        password = request.form.get('password', '')
        if len(username) < 3:
            flash('Username must be at least 3 characters long.', 'error')
            return render_template('register.html')
        if len(password) < 6:
            flash('Password must be at least 6 characters long.', 'error')
            return render_template('register.html')
        if mongo.db.users.find_one({'email': email}):
            flash('Email already registered.', 'error')
        elif mongo.db.users.find_one({'username': username}):
            flash('Username already taken.', 'error')
        else:
            mongo.db.users.insert_one({
                'username': username,
                'email': email,
                'password': generate_password_hash(password),
                'avatar': f'https://ui-avatars.com/api/?name={username}&background=random&color=fff',
                'bio': '',
                'theme': 'light',
                'sound': True,
                'created_at': datetime.now(timezone.utc),
                'last_login': None
            })
            flash('Account created successfully! Please log in.', 'success')
            return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    username = session['user']['username']
    session.pop('user', None)
    flash(f'Goodbye, {username}!', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    if not IS_DB_AVAILABLE:
        flash('Database connection not available.', 'error')
        return render_template('dashboard.html', user=session.get('user', {}), friends=[], friend_requests=[], all_users=[], user_stats={})
    user = mongo.db.users.find_one({'email': session['user']['email']})
    friends = get_user_friends(session['user']['email'])
    friend_requests = get_friend_requests(session['user']['email'])
    all_users = get_all_users_for_discovery(session['user']['email'])
    user_stats = get_user_stats(session['user']['email'])
    return render_template('dashboard.html', user=user, friends=friends, friend_requests=friend_requests, all_users=all_users, user_stats=user_stats)

@app.route('/chat')
@login_required
def chat():
    room = request.args.get('room')
    if not room:
        flash('Invalid chat room.', 'error')
        return redirect(url_for('dashboard'))

    # If old email-based room is detected, convert and redirect to username-based room
    if '@' in room:
        new_room, migrated = convert_email_room_to_username_room_if_needed(room)
        return redirect(url_for('chat', room=new_room))

    # Do NOT auto-mark messages as read here (Option 2)
    # If DB not available, just render page with empty messages
    if not IS_DB_AVAILABLE:
        return render_template('chat.html', user=session['user'], messages=[], room=room)

    try:
        page = int(request.args.get('page', 1))
        per_page = 50
        skip = (page - 1) * per_page
        messages = list(mongo.db.messages.find({'room': room}).sort('timestamp', -1).skip(skip).limit(per_page))
        messages.reverse()
        processed_messages = []
        for msg in messages:
            if 'timestamp' in msg:
                if isinstance(msg['timestamp'], str):
                    try:
                        msg['timestamp'] = datetime.fromisoformat(msg['timestamp'].replace('Z', '+00:00'))
                    except Exception:
                        msg['timestamp'] = datetime.now(timezone.utc)
            else:
                msg['timestamp'] = datetime.now(timezone.utc)
            if msg.get('message_type') == 'file' and 'file_info' in msg:
                file_info = msg['file_info']
                required_fields = ['original_name', 'filename', 'file_path', 'file_size', 'file_type']
                for field in required_fields:
                    if field not in file_info:
                        if field == 'file_type':
                            file_info[field] = get_file_type(file_info.get('original_name', ''))
                        elif field == 'file_size':
                            file_info[field] = 0
                        else:
                            file_info[field] = 'unknown'
            if msg.get('message_type') == 'voice' and 'voice_info' in msg:
                voice_info = msg['voice_info']
                required_fields = ['filename', 'file_path', 'duration']
                for field in required_fields:
                    if field not in voice_info:
                        if field == 'duration':
                            voice_info[field] = 0.0
                        else:
                            voice_info[field] = 'unknown'
            processed_messages.append(msg)

        # Participants: if room is private_x-y, extract usernames and lookup user docs
        participants = []
        if room.startswith('private_'):
            try:
                parts = room[len('private_'):].split('-')
                if len(parts) >= 2:
                    left = parts[0]
                    right = '-'.join(parts[1:])
                    u1 = mongo.db.users.find_one({'username': left})
                    u2 = mongo.db.users.find_one({'username': right})
                    def find_by_normalized(token):
                        for u in mongo.db.users.find():
                            if normalize_username_for_room(u.get('username','')) == token:
                                return u
                        return None
                    if not u1:
                        u1 = find_by_normalized(left)
                    if not u2:
                        u2 = find_by_normalized(right)
                    if u1:
                        participants.append(u1)
                    if u2 and (not u1 or u2['email'] != u1.get('email')):
                        participants.append(u2)
            except Exception:
                pass

        return render_template('chat.html', user=session['user'], messages=processed_messages, room=room, participants=participants)
    except Exception as e:
        app.logger.error(f"Chat loading error: {e}")
        flash('Error loading chat messages.', 'error')
        return render_template('chat.html', user=session['user'], messages=[], room=room)

# ---------------------------
# Upload/avatar + file serving
# ---------------------------
@app.route('/upload_avatar', methods=['POST'])
@login_required
def upload_avatar():
    if 'avatar' not in request.files:
        return jsonify({'success': False, 'message': 'No file uploaded'}), 400
    file = request.files['avatar']
    if file.filename == '':
        return jsonify({'success': False, 'message': 'Empty filename'}), 400
    try:
        upload_result = cloudinary.uploader.upload(
            file,
            folder='enhanced_messenger/avatars',
            overwrite=True,
            resource_type='image',
            transformation={'width': 512, 'height': 512, 'crop': 'limit'}
        )
        avatar_url = upload_result.get('secure_url')
        if IS_DB_AVAILABLE and mongo:
            mongo.db.users.update_one({'email': session['user']['email']}, {'$set': {'avatar': avatar_url}})
        session['user']['avatar'] = avatar_url
        return jsonify({'success': True, 'avatar_url': avatar_url})
    except Exception as e:
        app.logger.error(f"Avatar upload error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/uploads/<path:filename>')
def serve_file(filename):
    try:
        if '..' in filename or filename.startswith('/'):
            return "Access denied", 403
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if not os.path.exists(file_path):
            app.logger.debug(f"File not found: {file_path}")
            return "File not found", 404
        ext = filename.rsplit('.', 1)[-1].lower()
        if ext == "webm":
            mime_type = "audio/webm"
        elif ext == "wav":
            mime_type = "audio/wav"
        elif ext == "mp3":
            mime_type = "audio/mpeg"
        else:
            mime_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
        return send_file(file_path, mimetype=mime_type, as_attachment=False)
    except Exception as e:
        app.logger.error(f"❌ Error serving file {filename}: {e}")
        return "Server error", 500

@app.route('/uploads/thumbnails/<filename>')
def serve_thumbnail(filename):
    try:
        thumbnail_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'thumbnails')
        file_path = os.path.join(thumbnail_dir, filename)
        if not os.path.exists(file_path):
            return "Thumbnail not found", 404
        return send_file(file_path, mimetype='image/jpeg')
    except Exception as e:
        app.logger.error(f"Error serving thumbnail {filename}: {e}")
        return "Server error", 500

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'No file selected'})
    file = request.files['file']
    room = request.form.get('room')
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No file selected'})
    if not room:
        return jsonify({'success': False, 'message': 'Room required'})
    try:
        upload_result = cloudinary.uploader.upload(file, resource_type="auto")
        file_url = upload_result.get("secure_url")
        file_type = upload_result.get("resource_type")
        public_id = upload_result.get("public_id")
        file_size = upload_result.get("bytes")
        file_data = {
            'room': room,
            'author_username': session['user']['username'],
            'author_email': session['user']['email'],
            'message_type': 'file',
            'file_info': {
                'file_url': file_url,
                'file_path': file_url,
                'file_type': file_type,
                'public_id': public_id,
                'file_size': file_size
            },
            'text': "📎 Shared a file",
            'timestamp': datetime.now(timezone.utc)
        }
        if IS_DB_AVAILABLE:
            mongo.db.messages.insert_one(file_data)

        # emit message to room
        socketio.emit('new_message', {
            'author_username': file_data['author_username'],
            'text': file_data['text'],
            'timestamp': file_data['timestamp'].isoformat(),
            'message_type': 'file',
            'file_info': file_data['file_info']
        }, room=room)

        # Update unread counts for participants
        if IS_DB_AVAILABLE:
            update_unread_for_room(room, file_data['timestamp'], file_data['author_email'])

        return jsonify({'success': True, 'file_info': file_data['file_info']})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/download/<filename>')
@login_required
def download_file(filename):
    try:
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)
    except FileNotFoundError:
        flash('File not found', 'error')
        return redirect(url_for('dashboard'))

@app.route('/upload_voice', methods=['POST'])
@login_required
def upload_voice_message():
    if 'audio' not in request.files:
        return jsonify({'success': False, 'message': 'No audio file'})

    audio_file = request.files['audio']
    room = request.form.get('room')
    duration = float(request.form.get('duration', '0'))

    if not room:
        return jsonify({'success': False, 'message': 'Room required'})

    try:
        # --- Upload to Cloudinary ---
        upload_result = cloudinary.uploader.upload(
            audio_file,
            resource_type="video",   # Cloudinary treats audio/webm as video
            folder="enhanced_messenger/voice_messages",
            allowed_formats=["webm", "mp3", "wav", "ogg", "m4a"]
        )

        file_url = upload_result.get("secure_url")
        public_id = upload_result.get("public_id")
        file_size = upload_result.get("bytes")

        # Message data
        voice_data = {
            'room': room,
            'author_username': session['user']['username'],
            'author_email': session['user']['email'],
            'message_type': 'voice',
            'voice_info': {
                'file_url': file_url,
                'file_path': file_url,
                'public_id': public_id,
                'duration': duration,
                'file_size': file_size
            },
            'text': f"🎤 Voice message ({duration:.1f}s)",
            'timestamp': datetime.now(timezone.utc)
        }

        # Save to DB
        if IS_DB_AVAILABLE:
            result = mongo.db.messages.insert_one(voice_data)
            voice_data['_id'] = result.inserted_id

        # Emit over SocketIO
        socketio.emit('new_message', {
            'author_username': voice_data['author_username'],
            'text': voice_data['text'],
            'timestamp': voice_data['timestamp'].isoformat(),
            'message_type': 'voice',
            'voice_info': voice_data['voice_info']
        }, room=room)

        # Update unread counts for participants
        if IS_DB_AVAILABLE:
            update_unread_for_room(room, voice_data['timestamp'], voice_data['author_email'])

        return jsonify({'success': True, 'voice_info': voice_data['voice_info']})

    except Exception as e:
        app.logger.error(f"Voice upload error: {e}")
        return jsonify({'success': False, 'message': f"Upload failed: {str(e)}"})
 # ---------------------------
# MESSAGE EDIT / DELETE ENDPOINTS (FULLY FIXED)
# ---------------------------

@app.route('/edit_message', methods=['POST'])
@login_required
def edit_message():
    if not IS_DB_AVAILABLE:
        return jsonify({'success': False, 'message': 'DB disabled'})

    data = request.get_json()
    msg_id = data.get("message_id")
    new_text = data.get("new_text", "").strip()

    if not msg_id or not new_text:
        return jsonify({'success': False, 'message': 'Invalid data'})

    try:
        mongo.db.messages.update_one(
            {"_id": msg_id},
            {"$set": {"text": new_text}}
        )
        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/delete_message', methods=['POST'])
@login_required
def delete_message():
    if not IS_DB_AVAILABLE:
        return jsonify({'success': False, 'message': 'DB disabled'})

    data = request.get_json()
    ids = data.get("message_ids", [])

    try:
        for msg_id in ids:
            mongo.db.messages.delete_one({"_id": msg_id})

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/delete_everyone', methods=['POST'])
@login_required
def delete_everyone():
    if not IS_DB_AVAILABLE:
        return jsonify({'success': False, 'message': 'DB disabled'})

    data = request.get_json()
    msg_id = data.get("message_id")

    try:
        mongo.db.messages.delete_one({"_id": msg_id})
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/clear_chat', methods=['POST'])
@login_required
def clear_chat():
    if not IS_DB_AVAILABLE:
        return jsonify({'success': False, 'message': 'DB disabled'})

    data = request.get_json()
    room = data.get("room")

    try:
        mongo.db.messages.delete_many({"room": room})
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

# ---------------------------
# THEME UPDATE ENDPOINT (REQUIRED BY chat.js)
# ---------------------------
@app.route('/api/theme', methods=['POST'])
@login_required
def api_theme():
    if not IS_DB_AVAILABLE:
        return jsonify({'success': False, 'message': 'DB disabled'})

    data = request.get_json()
    theme = data.get('theme', 'light')

    try:
        mongo.db.users.update_one(
            {'email': session['user']['email']},
            {'$set': {'theme': theme}}
        )
        session['user']['theme'] = theme
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


# ---------------------------
# SocketIO events
# ---------------------------
@socketio.on('connect')
def on_connect():
    if 'user' in session:
        user_info = {'email': session['user']['email'], 'username': session['user']['username'], 'connected_at': datetime.now(timezone.utc)}
        active_users[request.sid] = user_info
        app.logger.info(f"✅ {session['user']['username']} connected (SID: {request.sid})")
        emit('user_status_change', {'email': user_info['email'], 'username': user_info['username'], 'status': 'online'}, broadcast=True)
    else:
        # if not authenticated, disconnect socket to avoid anonymous state
        disconnect()

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    if sid in active_users:
        user_info = active_users.pop(sid, None)
        if user_info:
            app.logger.info(f"❌ {user_info['username']} disconnected")
            for room in list(typing_users.keys()):
                typing_users[room].discard(sid)
                if not typing_users[room]:
                    del typing_users[room]
            emit('user_status_change', {'email': user_info['email'], 'username': user_info['username'], 'status': 'offline'}, broadcast=True)

@socketio.on('join_room')
def on_join_room(data):
    room = data.get('room')
    if room and 'user' in session:
        # if old email-based room passed through socket, convert
        if '@' in room:
            room, _ = convert_email_room_to_username_room_if_needed(room)
        join_room(room)
        user_rooms[request.sid] = room
        app.logger.debug(f"👥 {session['user']['username']} joined room: {room}")
        # IMPORTANT: For Option 2 we DO NOT mark messages as read upon join.
        emit('user_joined', {'username': session['user']['username'], 'room': room}, to=room, include_self=False)

@socketio.on('leave_room')
def on_leave_room(data):
    room = data.get('room')
    if room and request.sid in user_rooms:
        leave_room(room)
        user_rooms.pop(request.sid, None)
        app.logger.debug(f"👋 {session['user']['username']} left room: {room}")
        emit('user_left', {'username': session['user']['username'], 'room': room}, to=room)

@socketio.on('send_message')
def on_send_message(data):
    if 'user' not in session:
        return

    message_text = data.get('message', '').strip()
    room = data.get('room', '')
    if not message_text or not room:
        return

    if '@' in room:
        room, _ = convert_email_room_to_username_room_if_needed(room)

    message_data = {
        'room': room,
        'author_username': session['user']['username'],
        'author_email': session['user']['email'],
        'text': message_text,
        'timestamp': datetime.now(timezone.utc),
        'message_type': 'text'
    }

    if IS_DB_AVAILABLE:
        result = mongo.db.messages.insert_one(message_data)
        message_data['_id'] = result.inserted_id

    app.logger.debug(f"💬 Message from {session['user']['username']} in {room}: {message_text[:50]}...")
    emit('new_message', {
        'author_username': message_data['author_username'],
        'text': message_data['text'],
        'timestamp': message_data['timestamp'].isoformat(),
        'message_type': 'text'
    }, to=room)

    if IS_DB_AVAILABLE:
        try:
            update_unread_for_room(room, message_data['timestamp'], message_data['author_email'])
        except Exception as e:
            app.logger.debug(f"Failed to update unread for room {room}: {e}")


@socketio.on('typing')
def on_typing(data):
    if 'user' not in session:
        return
    room = data.get('room')
    if room:
        typing_users.setdefault(room, set()).add(request.sid)
        emit('user_typing', {'username': session['user']['username'], 'room': room}, to=room, include_self=False)

@socketio.on('stop_typing')
def on_stop_typing(data):
    if 'user' not in session:
        return
    room = data.get('room')
    if room and room in typing_users:
        typing_users[room].discard(request.sid)
        if not typing_users[room]:
            del typing_users[room]
        emit('user_stopped_typing', {'username': session['user']['username'], 'room': room}, to=room, include_self=False)

@socketio.on('get_online_users')
def on_get_online_users():
    online_users = [{'username': u['username'], 'email': u['email']} for u in active_users.values()]
    emit('online_users', {'users': online_users})

@socketio.on('mark_read')
def on_mark_read(data):
    if 'user' not in session:
        return

    room = data.get('room')
    user_email = session['user']['email']

    if not room:
        return

    # mark in DB
    mark_messages_as_read(user_email, room)

    # unread becomes zero for this user
    updated_count = get_unread_count(user_email, room)

    # send update to this user
    for sid in get_sids_for_email(user_email):
        emit('unread_update', {
            'room': room,
            'unread_count': updated_count
        }, to=sid)

    # notify other participant so they refresh UI also
    others = get_emails_for_room(room)
    for participant in others:
        if participant == user_email:
            continue
        count_other = get_unread_count(participant, room)
        for sid in get_sids_for_email(participant):
            emit('unread_update', {
                'room': room,
                'unread_count': count_other
            }, to=sid)


# ---------------------------
# Friend endpoints
# ---------------------------
@app.route('/add_friend', methods=['POST'])
@login_required
def add_friend():
    if not IS_DB_AVAILABLE:
        flash('Database connection not available.', 'error')
        return redirect(url_for('dashboard'))
    recipient_username = request.form.get('username', '').strip()
    recipient = mongo.db.users.find_one({'username': recipient_username})
    if not recipient:
        flash('User not found.', 'error')
    elif recipient['email'] == session['user']['email']:
        flash('You cannot add yourself as a friend.', 'error')
    else:
        existing_friendship = mongo.db.friends.find_one({
            '$or': [{'user1': session['user']['email'], 'user2': recipient['email']}, {'user1': recipient['email'], 'user2': session['user']['email']}]
        })
        if existing_friendship:
            flash('You are already friends with this user.', 'error')
        else:
            existing_request = mongo.db.friend_requests.find_one({'sender': session['user']['email'], 'recipient': recipient['email'], 'status': 'pending'})
            if existing_request:
                flash('Friend request already sent.', 'error')
            else:
                mongo.db.friend_requests.insert_one({'sender': session['user']['email'], 'recipient': recipient['email'], 'status': 'pending', 'sent_at': datetime.now(timezone.utc)})
                flash(f'Friend request sent to {recipient_username}!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/accept_friend', methods=['POST'])
@login_required
def accept_friend():
    if not IS_DB_AVAILABLE:
        flash('Database connection not available.', 'error')
        return redirect(url_for('dashboard'))
    sender_email = request.form.get('sender')
    recipient_email = session['user']['email']
    mongo.db.friend_requests.delete_one({'sender': sender_email, 'recipient': recipient_email})
    sender_user = mongo.db.users.find_one({'email': sender_email})
    recipient_user = mongo.db.users.find_one({'email': recipient_email})
    if sender_user and recipient_user:
        room = get_private_room_id(sender_user.get('username'), recipient_user.get('username'))
    else:
        room = get_private_room_id(sender_email, recipient_email)
    mongo.db.friends.insert_one({'user1': sender_email, 'user2': recipient_email, 'room': room, 'created_at': datetime.now(timezone.utc)})
    sender = mongo.db.users.find_one({'email': sender_email})
    sender_name = sender['username'] if sender else 'Unknown user'
    flash(f'You are now friends with {sender_name}!', 'success')
    return redirect(url_for('dashboard'))

# ---------------------------
# Settings, Profile, etc.
# ---------------------------
@app.route('/profile')
@login_required
def profile():
    if not IS_DB_AVAILABLE:
        return render_template('profile.html', user=session['user'], userdata={}, friends=[], friend_requests=[], message_count=0)
    user = mongo.db.users.find_one({'email': session['user']['email']})
    friends = get_user_friends(session['user']['email'])
    friend_requests = get_friend_requests(session['user']['email'])
    message_count = mongo.db.messages.count_documents({'author_email': session['user']['email']})
    return render_template('profile.html', user=session['user'], userdata=user, friends=friends, friend_requests=friend_requests, message_count=message_count)

@app.route('/settings', methods=['GET','POST'])
@login_required
def settings():
    if not IS_DB_AVAILABLE:
        flash('Database connection not available.', 'error')
        return render_template('settings.html', user=session['user'], userdata={})
    user = mongo.db.users.find_one({'email': session['user']['email']})
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'change_password':
            current_password = request.form.get('current_password')
            new_password = request.form.get('new_password')
            if not check_password_hash(user['password'], current_password):
                flash('Current password is incorrect.', 'error')
            elif len(new_password) < 6:
                flash('New password must be at least 6 characters long.', 'error')
            else:
                mongo.db.users.update_one({'email': session['user']['email']}, {'$set': {'password': generate_password_hash(new_password)}})
                flash('Password updated successfully!', 'success')
        else:
            updates = {}
            if 'username' in request.form:
                new_username = request.form.get('username').strip()
                if new_username and new_username != user['username']:
                    if mongo.db.users.find_one({'username': new_username, 'email': {'$ne': session['user']['email']}}):
                        flash('Username already taken.', 'error')
                    else:
                        updates['username'] = new_username
                        session['user']['username'] = new_username
            if 'bio' in request.form:
                updates['bio'] = request.form.get('bio', '')
            if 'theme' in request.form:
                updates['theme'] = request.form.get('theme', 'light')
            updates['sound'] = 'notification_sound' in request.form
            if updates:
                mongo.db.users.update_one({'email': session['user']['email']}, {'$set': updates})
                flash('Settings updated successfully!', 'success')
        return redirect(url_for('settings'))
    return render_template('settings.html', user=session['user'], userdata=user)

# ---------------------------
# Cleanup functions (do not start on Gunicorn)
# ---------------------------
def cleanup_old_files():
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        return
    cutoff_time = datetime.now() - timedelta(days=30)
    try:
        for filename in os.listdir(app.config['UPLOAD_FOLDER']):
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            if os.path.isfile(file_path):
                file_time = datetime.fromtimestamp(os.path.getctime(file_path))
                if file_time < cutoff_time:
                    try:
                        os.remove(file_path)
                        app.logger.info(f"Cleaned up old file: {filename}")
                    except Exception as e:
                        app.logger.warning(f"Failed to remove old file {filename}: {e}")
    except Exception as e:
        app.logger.warning(f"Cleanup error: {e}")

def cleanup_inactive_sessions():
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
    inactive_sids = []
    for sid, user_data in list(active_users.items()):
        connected_at = user_data.get('connected_at', datetime.now(timezone.utc))
        if connected_at < cutoff_time:
            inactive_sids.append(sid)
    for sid in inactive_sids:
        active_users.pop(sid, None)
        user_rooms.pop(sid, None)

def enhanced_background_cleanup(stop_event):
    while not stop_event.is_set():
        stop_event.wait(3600)
        try:
            cleanup_inactive_sessions()
            now = datetime.now()
            if now.hour == 2:
                cleanup_old_files()
        except Exception as e:
            app.logger.debug(f"Background cleanup loop error: {e}")

_bg_stop_event = None
_bg_thread = None

# ---------------------------
# Context processors, filters and error handlers
# ---------------------------
@app.context_processor
def inject_theme_info():
    user_theme = 'light'
    if 'user' in session and IS_DB_AVAILABLE:
        try:
            user_data = mongo.db.users.find_one({'email': session['user']['email']})
            if user_data and 'theme' in user_data:
                user_theme = user_data['theme']
        except Exception:
            pass
    return {'user_theme': user_theme, 'active_users_count': len(active_users), 'db_available': IS_DB_AVAILABLE, 'app_version': '2.2.0'}

@app.context_processor
def inject_debug_info():
    return {'active_users_count': len(active_users), 'db_available': IS_DB_AVAILABLE, 'app_version': '2.1.0'}

@app.template_filter('timeago')
def timeago_filter(dt):
    if not dt:
        return "Unknown"
    now = datetime.now(timezone.utc)
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except Exception:
            return "Unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    seconds = diff.total_seconds()
    if seconds < 60:
        return "Just now"
    elif seconds < 3600:
        mins = int(seconds // 60)
        return f"{mins} minute{'s' if mins > 1 else ''} ago"
    elif seconds < 86400:
        hrs = int(seconds // 3600)
        return f"{hrs} hour{'s' if hrs > 1 else ''} ago"
    elif seconds < 2592000:
        days = int(seconds // 86400)
        return f"{days} day{'s' if days > 1 else ''} ago"
    else:
        return dt.strftime('%b %d, %Y')

@app.template_filter('format_time')
def format_time_filter(dt):
    if not dt:
        return 'Now'
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except Exception:
            return dt
    try:
        return dt.strftime('%I:%M %p')
    except Exception:
        return str(dt)

@app.template_filter('file_size')
def file_size_filter(size_bytes):
    if not size_bytes:
        return '0 B'
    try:
        size_bytes = float(size_bytes)
        for unit in ['B','KB','MB','GB']:
            if size_bytes < 1024:
                return f'{size_bytes:.1f} {unit}'
            size_bytes /= 1024
        return f'{size_bytes:.1f} TB'
    except Exception:
        return '0 B'

# ---------------------------
# Error handlers
# ---------------------------
@app.errorhandler(413)
def file_too_large(error):
    flash('File too large. Maximum file size is 100MB.', 'error')
    return redirect(request.referrer or url_for('dashboard'))

@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/uploads/"):
        return "File not found", 404
    flash('Page not found.', 'error')
    return redirect(url_for('dashboard'))

@app.errorhandler(500)
def internal_error(error):
    app.logger.error(f"Internal server error: {error}")
    flash('Something went wrong. Please try again.', 'error')
    return redirect(url_for('dashboard'))

# ---------------------------
# Main (dev run only)
# ---------------------------
if __name__ == '__main__':
    app.logger.info("🚀 Starting Enhanced Messenger App (dev mode)...")
    app.logger.info(f"📊 Database: {'Connected' if IS_DB_AVAILABLE else 'Disabled'}")
    app.logger.info("Starting local background cleanup thread (dev only)")
    _bg_stop_event = threading.Event()
    _bg_thread = threading.Thread(target=enhanced_background_cleanup, args=(_bg_stop_event,), daemon=True)
    _bg_thread.start()
    try:
        port = int(os.environ.get("PORT", 5000))
    except Exception:
        port = 5000
    socketio.run(app, host='0.0.0.0', port=port, debug=True)






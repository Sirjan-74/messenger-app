import eventlet
eventlet.monkey_patch()

import os
import secrets
import base64
import mimetypes
import threading
import time
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
    # Keep logs minimal in production
    print("ERROR: PyMongo not installed. Run `pip install flask-pymongo` if using DB support")

from flask_socketio import SocketIO, emit, join_room, leave_room

# Flask app config
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'static/uploads')
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 100 * 1024 * 1024))  # 100MB default
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # disable caching for development

# Allowed extensions
ALLOWED_EXTENSIONS = set(os.environ.get(
    "ALLOWED_EXTENSIONS",
    "txt,pdf,png,jpg,jpeg,gif,mp4,mp3,wav,doc,docx,xls,xlsx,ppt,pptx,zip,rar,webm"
).split(","))

# ensure upload directories exist in repo (or add .gitkeep)
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
            # safe to create indexes if not exist
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

# SocketIO + eventlet
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet", manage_session=True)

# In-memory state
active_users = {}
user_rooms = {}
typing_users = {}

# ---------------------------
# Utility functions
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
    """Create thumbnail for images; return URL path e.g. /uploads/thumbnails/thumb_name.jpg"""
    if not PIL_AVAILABLE:
        app.logger.debug("Pillow not available — skipping thumbnail creation")
        return None
    try:
        with Image.open(file_path) as img:
            # convert to RGB if needed
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = background
            # choose resampling attr compatibly
            try:
                resample = Image.Resampling.LANCZOS
            except Exception:
                # Pillow < 9 fallback
                resample = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.ANTIALIAS
            img.thumbnail((200, 200), resample)
            thumbnail_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'thumbnails')
            os.makedirs(thumbnail_dir, exist_ok=True)
            thumbnail_filename = f"thumb_{filename}"
            thumbnail_path = os.path.join(thumbnail_dir, thumbnail_filename)
            img.save(thumbnail_path, 'JPEG', quality=85)
            # Return URL path with leading slash
            return f"/uploads/thumbnails/{thumbnail_filename}"
    except Exception as e:
        app.logger.debug(f"Thumbnail creation failed for {filename}: {e}")
        return None

# ---------------------------
# DB helper functions (only used when DB is enabled)
# ---------------------------
def get_unread_count(user_email, room):
    if not IS_DB_AVAILABLE:
        return 0
    user_room_data = mongo.db.user_rooms.find_one({'user_email': user_email, 'room': room})
    last_read = user_room_data.get('last_read') if user_room_data else datetime.min
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

def get_private_room_id(email1, email2):
    return f"private_{'-'.join(sorted([email1, email2]))}"

def get_user_friends(email):
    if not IS_DB_AVAILABLE:
        return []
    friends = []
    for rel in mongo.db.friends.find({'$or': [{'user1': email}, {'user2': email}]}):
        friend_email = rel['user2'] if rel['user1'] == email else rel['user1']
        fuser = mongo.db.users.find_one({'email': friend_email})
        if fuser:
            is_online = any(u['email'] == friend_email for u in active_users.values())
            last_message = mongo.db.messages.find_one({'room': rel.get('room')}, sort=[('timestamp', -1)])
            friends.append({
                'username': fuser['username'],
                'email': friend_email,
                'avatar': fuser.get('avatar', f'https://ui-avatars.com/api/?name={fuser["username"]}&background=random&color=fff'),
                'is_online': is_online,
                'room': rel.get('room'),
                'last_message': last_message.get('text', '') if last_message else '',
                'last_message_time': last_message.get('timestamp') if last_message else None,
                'unread_count': get_unread_count(email, rel.get('room'))
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
            user_rooms_set.add(rel['room'])
    total_messages_in_conversations = mongo.db.messages.count_documents({'room': {'$in': list(user_rooms_set)}}) if user_rooms_set else 0
    messages_received = total_messages_in_conversations - messages_sent
    user_data = mongo.db.users.find_one({'email': user_email})
    join_date = user_data.get('created_at', datetime.now(timezone.utc)) if user_data else datetime.now(timezone.utc)
    return {'friends_count': friends_count, 'messages_sent': messages_sent, 'messages_received': max(0, messages_received), 'total_conversations': len(user_rooms_set), 'join_date': join_date}

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
        return render_template('dashboard.html', user=session['user'], friends=[], friend_requests=[], all_users=[], user_stats={})
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
    if not IS_DB_AVAILABLE:
        return render_template('chat.html', user=session['user'], messages=[], room=room)
    mark_messages_as_read(session['user']['email'], room)
    page = int(request.args.get('page', 1))
    per_page = 50
    skip = (page - 1) * per_page
    try:
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
        participants = []
        if room.startswith('private_'):
            room_emails = room.replace('private_', '').split('-')
            for email in room_emails:
                if email != session['user']['email']:
                    participant = mongo.db.users.find_one({'email': email})
                    if participant:
                        participants.append(participant)
        return render_template('chat.html', user=session['user'], messages=processed_messages, room=room, participants=participants)
    except Exception as e:
        app.logger.error(f"Chat loading error: {e}")
        flash('Error loading chat messages.', 'error')
        return render_template('chat.html', user=session['user'], messages=[], room=room)

# ------------- Theme API -------------
@app.route('/api/theme', methods=['POST'])
@login_required
def update_theme():
    if not IS_DB_AVAILABLE:
        return jsonify({'success': False, 'message': 'Database not available'})
    data = request.get_json()
    theme = data.get('theme', 'light')
    if theme not in ['light', 'dark']:
        return jsonify({'success': False, 'message': 'Invalid theme'})
    try:
        mongo.db.users.update_one({'email': session['user']['email']}, {'$set': {'theme': theme}})
        app.logger.info(f"✅ Theme updated for {session['user']['email']}: {theme}")
        return jsonify({'success': True, 'message': 'Theme updated', 'theme': theme})
    except Exception as e:
        app.logger.error(f"❌ Theme update error: {e}")
        return jsonify({'success': False, 'message': 'Update failed'})

# ------------- File upload / serve -------------
@app.route('/uploads/<path:filename>')
def serve_file(filename):
    try:
        if '..' in filename or filename.startswith('/'):
            return "Access denied", 403
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if not os.path.exists(file_path):
            app.logger.debug(f"File not found: {file_path}")
            return "File not found", 404
        mime_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
        return send_file(file_path, mimetype=mime_type, as_attachment=False, download_name=os.path.basename(file_path))
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
    if file and allowed_file(file.filename):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = secure_filename(file.filename)
        unique_filename = f"{timestamp}_{filename}"
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        try:
            file.save(file_path)
            file_size = os.path.getsize(file_path)
            file_type = get_file_type(filename)
            mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            file_url = f"/uploads/{unique_filename}"
            thumbnail_url = None
            if file_type == 'image':
                thumbnail_url = create_thumbnail(file_path, unique_filename)
            file_data = {
                'room': room,
                'author_username': session['user']['username'],
                'author_email': session['user']['email'],
                'message_type': 'file',
                'file_info': {
                    'original_name': filename,
                    'filename': unique_filename,
                    'file_path': file_url,
                    'file_size': file_size,
                    'file_type': file_type,
                    'mime_type': mime_type,
                    'thumbnail': thumbnail_url
                },
                'text': f"📎 Shared a {file_type}: {filename}",
                'timestamp': datetime.now(timezone.utc)
            }
            if IS_DB_AVAILABLE:
                result = mongo.db.messages.insert_one(file_data)
                file_data['_id'] = result.inserted_id
            app.logger.info(f"📎 File uploaded: {filename} -> {file_url}")
            socketio.emit('new_message', {
                'author_username': file_data['author_username'],
                'text': file_data['text'],
                'timestamp': file_data['timestamp'].isoformat(),
                'message_type': 'file',
                'file_info': file_data['file_info']
            }, room=room)
            return jsonify({'success': True, 'message': 'File uploaded successfully', 'file_info': file_data['file_info']})
        except Exception as e:
            app.logger.error(f"❌ File upload error: {e}")
            return jsonify({'success': False, 'message': f'Upload failed: {str(e)}'})
    return jsonify({'success': False, 'message': 'File type not allowed'})

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
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_username = secure_filename(session['user']['username'])
        filename = f"voice_{timestamp}_{safe_username}.webm"
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        audio_file.save(file_path)
        file_size = os.path.getsize(file_path)
        file_url = f"/uploads/{filename}"
        voice_data = {
            'room': room,
            'author_username': session['user']['username'],
            'author_email': session['user']['email'],
            'message_type': 'voice',
            'voice_info': {'filename': filename, 'file_path': file_url, 'duration': duration, 'file_size': file_size},
            'text': f"🎤 Voice message ({duration:.1f}s)",
            'timestamp': datetime.now(timezone.utc)
        }
        if IS_DB_AVAILABLE:
            result = mongo.db.messages.insert_one(voice_data)
            voice_data['_id'] = result.inserted_id
        app.logger.info(f"🎤 Voice message saved: {filename} -> {file_url}")
        socketio.emit('new_message', {
            'author_username': voice_data['author_username'],
            'text': voice_data['text'],
            'timestamp': voice_data['timestamp'].isoformat(),
            'message_type': 'voice',
            'voice_info': voice_data['voice_info']
        }, room=room)
        return jsonify({'success': True, 'message': 'Voice message sent', 'voice_info': voice_data['voice_info']})
    except Exception as e:
        app.logger.error(f"❌ Voice upload error: {e}")
        return jsonify({'success': False, 'message': f'Voice upload failed: {str(e)}'})

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

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    if sid in active_users:
        user_info = active_users.pop(sid, None)
        if user_info:
            app.logger.info(f"❌ {user_info['username']} disconnected")
            # clean typing state
            for room in list(typing_users.keys()):
                typing_users[room].discard(sid)
                if not typing_users[room]:
                    del typing_users[room]
            emit('user_status_change', {'email': user_info['email'], 'username': user_info['username'], 'status': 'offline'}, broadcast=True)

@socketio.on('join_room')
def on_join_room(data):
    room = data.get('room')
    if room and 'user' in session:
        join_room(room)
        user_rooms[request.sid] = room
        app.logger.debug(f"👥 {session['user']['username']} joined room: {room}")
        if IS_DB_AVAILABLE:
            mark_messages_as_read(session['user']['email'], room)
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
    message_data = {'room': room, 'author_username': session['user']['username'], 'author_email': session['user']['email'], 'text': message_text, 'timestamp': datetime.now(timezone.utc), 'message_type': 'text'}
    if IS_DB_AVAILABLE:
        result = mongo.db.messages.insert_one(message_data)
        message_data['_id'] = result.inserted_id
    app.logger.debug(f"💬 Message from {session['user']['username']} in {room}: {message_text[:50]}...")
    emit('new_message', {'author_username': message_data['author_username'], 'text': message_data['text'], 'timestamp': message_data['timestamp'].isoformat(), 'message_type': 'text'}, to=room)

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
# Cleanup functions (do not start automatically on Render/Gunicorn)
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
        # Run hourly
        stop_event.wait(3600)
        try:
            cleanup_inactive_sessions()
            # daily file cleanup at 2AM local time
            now = datetime.now()
            if now.hour == 2:
                cleanup_old_files()
        except Exception as e:
            app.logger.debug(f"Background cleanup loop error: {e}")

# We DO NOT start the background thread automatically in production (gunicorn).
# Start it only in local dev (__main__) so it won't interfere with Gunicorn/eventlet monkey patch.
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
# Error handlers (single set)
# ---------------------------
@app.errorhandler(413)
def file_too_large(error):
    flash('File too large. Maximum file size is 100MB.', 'error')
    return redirect(request.referrer or url_for('dashboard'))

@app.errorhandler(404)
def not_found(error):
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
    # start background thread only in dev (__main__)
    _bg_stop_event = threading.Event()
    _bg_thread = threading.Thread(target=enhanced_background_cleanup, args=(_bg_stop_event,), daemon=True)
    _bg_thread.start()
    try:
        port = int(os.environ.get("PORT", 5000))
    except Exception:
        port = 5000
    socketio.run(app, host='0.0.0.0', port=port, debug=True)


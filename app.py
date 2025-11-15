import os
import secrets
import base64 
from werkzeug.utils import secure_filename 
from PIL import Image
import mimetypes
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import generate_password_hash, check_password_hash
from flask import send_file

# MongoDB Setup
IS_DB_AVAILABLE = False
try:
    from flask_pymongo import PyMongo
    IS_DB_AVAILABLE = True
except ImportError:
    print("ERROR: PyMongo not installed. Run `pip install flask-pymongo`")

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max file size
ALLOWED_EXTENSIONS = {
    'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'mp4', 'mp3', 'wav', 
    'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'zip', 'rar', 'webm'
}
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # Disable caching for development

# Create upload directories
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'thumbnails'), exist_ok=True)

mongo = None

if IS_DB_AVAILABLE:
    app.config["MONGO_URI"] = "mongodb+srv://sirjannishad74:Sirjan3008@cluster0.ep61yjv.mongodb.net/messengerApp?retryWrites=true&w=majority&appName=Cluster0"
    try:
        mongo = PyMongo(app)
        mongo.db.command('ping')
        print("Successfully connected to MongoDB")
        
        # Create indexes for better performance
        mongo.db.messages.create_index([("room", 1), ("timestamp", -1)])
        mongo.db.users.create_index([("email", 1)], unique=True)
        mongo.db.friends.create_index([("user1", 1), ("user2", 1)])
        
    except Exception as e:
        print(f"MongoDB connection error: {e}")
        IS_DB_AVAILABLE = False
else:
    print("Database support disabled.")

socketio = SocketIO(app, manage_session=True, cors_allowed_origins="*")

# Active users tracking
active_users = {}
user_rooms = {}
typing_users = {}

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if 'user' not in session:
            flash('Please log in first.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapped

def get_user_friends(email):
    """Get user's friends with enhanced information"""
    if not IS_DB_AVAILABLE:
        return []
    
    friends = []
    for rel in mongo.db.friends.find({'$or': [{'user1': email}, {'user2': email}]}):
        friend_email = rel['user2'] if rel['user1'] == email else rel['user1']
        fuser = mongo.db.users.find_one({'email': friend_email})
        
        if fuser:
            # Check if friend is online
            is_online = any(u['email'] == friend_email for u in active_users.values())
            
            # Get last message in this room
            last_message = mongo.db.messages.find_one(
                {'room': rel.get('room')},
                sort=[('timestamp', -1)]
            )
            
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
    
    # Sort friends by online status and last message time
    friends.sort(key=lambda f: (not f['is_online'], f['last_message_time'] or datetime.min), reverse=True)
    return friends

def get_unread_count(user_email, room):
    """Get unread message count for a user in a room"""
    if not IS_DB_AVAILABLE:
        return 0
    
    # Get user's last read timestamp for this room
    user_room_data = mongo.db.user_rooms.find_one({
        'user_email': user_email,
        'room': room
    })
    
    last_read = user_room_data.get('last_read') if user_room_data else datetime.min
    
    # Count unread messages
    unread_count = mongo.db.messages.count_documents({
        'room': room,
        'timestamp': {'$gt': last_read},
        'author_email': {'$ne': user_email}  # Don't count own messages
    })
    
    return unread_count

def mark_messages_as_read(user_email, room):
    """Mark messages as read for a user in a room"""
    if not IS_DB_AVAILABLE:
        return
    
    mongo.db.user_rooms.update_one(
        {'user_email': user_email, 'room': room},
        {
            '$set': {
                'last_read': datetime.now(timezone.utc),
                'user_email': user_email,
                'room': room
            }
        },
        upsert=True
    )

def get_friend_requests(email):
    """Get pending friend requests"""
    if not IS_DB_AVAILABLE:
        return []
    
    requests = []
    for fr in mongo.db.friend_requests.find({'recipient': email, 'status': 'pending'}):
        sender = mongo.db.users.find_one({'email': fr['sender']})
        if sender:
            requests.append({
                'sender': fr['sender'],
                'sender_username': sender['username'],
                'sent_at': fr.get('sent_at', datetime.utcnow())
            })
    
    return requests

def get_private_room_id(email1, email2):
    """Generate consistent room ID for private chat"""
    return f"private_{'-'.join(sorted([email1, email2]))}"

def get_all_users_for_discovery(current_user_email):
    """Get all users except current user with their relationship status"""
    if not IS_DB_AVAILABLE:
        return []
    
    # Get current user's friends
    friend_emails = set()
    for rel in mongo.db.friends.find({'$or': [{'user1': current_user_email}, {'user2': current_user_email}]}):
        friend_email = rel['user2'] if rel['user1'] == current_user_email else rel['user1']
        friend_emails.add(friend_email)
    
    # Get pending friend requests (both sent and received)
    pending_requests = set()
    for req in mongo.db.friend_requests.find({
        '$or': [
            {'sender': current_user_email, 'status': 'pending'},
            {'recipient': current_user_email, 'status': 'pending'}
        ]
    }):
        pending_requests.add(req['sender'] if req['sender'] != current_user_email else req['recipient'])
    
    # Get all users except current user
    all_users = []
    users_cursor = mongo.db.users.find({'email': {'$ne': current_user_email}}).sort('username', 1)
    
    for user in users_cursor:
        # Check if user is online
        is_online = any(u['email'] == user['email'] for u in active_users.values())
        
        # Determine relationship status
        if user['email'] in friend_emails:
            status = 'friends'
        elif user['email'] in pending_requests:
            status = 'pending'
        else:
            status = 'none'
        
        # Only show non-friends for discovery (friends are shown in main friends list)
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
    """Get comprehensive user statistics"""
    if not IS_DB_AVAILABLE:
        return {
            'friends_count': 0,
            'messages_sent': 0,
            'messages_received': 0,
            'total_conversations': 0,
            'join_date': datetime.now(timezone.utc)
        }
    
    # Get friends count
    friends_count = mongo.db.friends.count_documents({
        '$or': [{'user1': user_email}, {'user2': user_email}]
    })
    
    # Get messages sent
    messages_sent = mongo.db.messages.count_documents({'author_email': user_email})
    
    # Get total messages in user's conversations
    user_rooms = set()
    for rel in mongo.db.friends.find({'$or': [{'user1': user_email}, {'user2': user_email}]}):
        if rel.get('room'):
            user_rooms.add(rel['room'])
    
    total_messages_in_conversations = mongo.db.messages.count_documents({
        'room': {'$in': list(user_rooms)}
    }) if user_rooms else 0
    
    messages_received = total_messages_in_conversations - messages_sent
    
    # Get user join date
    user_data = mongo.db.users.find_one({'email': user_email})
    join_date = user_data.get('created_at', datetime.now(timezone.utc)) if user_data else datetime.now(timezone.utc)
    
    return {
        'friends_count': friends_count,
        'messages_sent': messages_sent,
        'messages_received': max(0, messages_received),
        'total_conversations': len(user_rooms),
        'join_date': join_date
    }

def allowed_file(filename):
    """Check if file extension is allowed"""
    if not filename or '.' not in filename:
        return False
    
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS

def safe_file_operation(operation, *args, **kwargs):
    """Safely execute file operations with error handling"""
    try:
        return operation(*args, **kwargs)
    except Exception as e:
        print(f"File operation error: {e}")
        return None


def create_thumbnail(file_path, filename):
    """Create thumbnail for images with proper error handling"""
    try:
        if get_file_type(filename) == 'image':
            with Image.open(file_path) as img:
                # Convert to RGB if necessary (for PNG with transparency)
                if img.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                    img = background
                
                img.thumbnail((200, 200), Image.Resampling.LANCZOS)
                
                thumbnail_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'thumbnails')
                os.makedirs(thumbnail_dir, exist_ok=True)
                thumbnail_filename = f"thumb_{filename}"
                thumbnail_path = os.path.join(thumbnail_dir, thumbnail_filename)
                
                img.save(thumbnail_path, 'JPEG', quality=85)
                
                # Return URL path without leading slash
                return f"uploads/thumbnails/{thumbnail_filename}"
                
    except Exception as e:
        print(f"Thumbnail creation failed for {filename}: {e}")
    
    return None

@app.route('/uploads/thumbnails/<filename>')
def serve_thumbnail(filename):
    """Serve thumbnail files"""
    try:
        thumbnail_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'thumbnails')
        file_path = os.path.join(thumbnail_dir, filename)
        
        if not os.path.exists(file_path):
            return "Thumbnail not found", 404
        
        return send_file(file_path, mimetype='image/jpeg')
    except Exception as e:
        print(f"Error serving thumbnail {filename}: {e}")
        return "Server error", 500

# Routes
@app.route('/')
def home():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    else:
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
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
            
            # Update last login
            mongo.db.users.update_one(
                {'email': email},
                {'$set': {'last_login': datetime.now(timezone.utc)}}
            )
            
            flash(f'Welcome back, {user["username"]}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password.', 'error')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
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
        
        # Validation
        if len(username) < 3:
            flash('Username must be at least 3 characters long.', 'error')
            return render_template('register.html')
        
        if len(password) < 6:
            flash('Password must be at least 6 characters long.', 'error')
            return render_template('register.html')
        
        # Check if user exists
        if mongo.db.users.find_one({'email': email}):
            flash('Email already registered.', 'error')
        elif mongo.db.users.find_one({'username': username}):
            flash('Username already taken.', 'error')
        else:
            # Create user
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
        return render_template('dashboard.html', 
                             user=session['user'], 
                             friends=[], 
                             friend_requests=[],
                             all_users=[],
                             user_stats={})
    
    user = mongo.db.users.find_one({'email': session['user']['email']})
    friends = get_user_friends(session['user']['email'])
    friend_requests = get_friend_requests(session['user']['email'])
    all_users = get_all_users_for_discovery(session['user']['email'])
    user_stats = get_user_stats(session['user']['email'])
    
    return render_template('dashboard.html', 
                         user=user, 
                         friends=friends, 
                         friend_requests=friend_requests,
                         all_users=all_users,
                         user_stats=user_stats)

@app.route('/chat')
@login_required  
def chat():
    room = request.args.get('room')
    if not room:
        flash('Invalid chat room.', 'error')
        return redirect(url_for('dashboard'))
    
    if not IS_DB_AVAILABLE:
        return render_template('chat.html', user=session['user'], messages=[], room=room)
    
    # Mark messages as read
    mark_messages_as_read(session['user']['email'], room)
    
    # Get messages with pagination
    page = int(request.args.get('page', 1))
    per_page = 50
    skip = (page - 1) * per_page
    
    try:
        messages = list(mongo.db.messages.find({'room': room})
                       .sort('timestamp', -1)
                       .skip(skip)
                       .limit(per_page))
        messages.reverse()  # Show oldest first
        
        # Process messages to ensure proper handling
        processed_messages = []
        for msg in messages:
            # Handle timestamp
            if 'timestamp' in msg:
                if isinstance(msg['timestamp'], str):
                    try:
                        msg['timestamp'] = datetime.fromisoformat(msg['timestamp'].replace('Z', '+00:00'))
                    except (ValueError, AttributeError):
                        msg['timestamp'] = datetime.now(timezone.utc)
            else:
                msg['timestamp'] = datetime.now(timezone.utc)
            
            # Ensure file info is properly structured
            if msg.get('message_type') == 'file' and 'file_info' in msg:
                file_info = msg['file_info']
                # Ensure all required fields exist
                required_fields = ['original_name', 'filename', 'file_path', 'file_size', 'file_type']
                for field in required_fields:
                    if field not in file_info:
                        if field == 'file_type':
                            file_info[field] = get_file_type(file_info.get('original_name', ''))
                        elif field == 'file_size':
                            file_info[field] = 0
                        else:
                            file_info[field] = 'unknown'
            
            # Ensure voice info is properly structured  
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
        
        # Get room participants for better context
        participants = []
        if room.startswith('private_'):
            room_emails = room.replace('private_', '').split('-')
            for email in room_emails:
                if email != session['user']['email']:
                    participant = mongo.db.users.find_one({'email': email})
                    if participant:
                        participants.append(participant)
        
        return render_template('chat.html', 
                             user=session['user'], 
                             messages=processed_messages, 
                             room=room,
                             participants=participants)
                             
    except Exception as e:
        print(f"Chat loading error: {e}")
        flash('Error loading chat messages.', 'error')
        return render_template('chat.html', user=session['user'], messages=[], room=room)


# 7. Fix theme API to save properly
@app.route('/api/theme', methods=['POST'])
@login_required
def update_theme():
    """API endpoint to update user theme preference"""
    if not IS_DB_AVAILABLE:
        return jsonify({'success': False, 'message': 'Database not available'})
    
    data = request.get_json()
    theme = data.get('theme', 'light')
    
    if theme not in ['light', 'dark']:
        return jsonify({'success': False, 'message': 'Invalid theme'})
    
    try:
        # Update database
        mongo.db.users.update_one(
            {'email': session['user']['email']},
            {'$set': {'theme': theme}}
        )
        
        print(f"✅ Theme updated for {session['user']['email']}: {theme}")
        
        return jsonify({'success': True, 'message': 'Theme updated', 'theme': theme})
    except Exception as e:
        print(f"❌ Theme update error: {e}")
        return jsonify({'success': False, 'message': 'Update failed'})
    
# Enhanced error handlers with better user feedback
@app.errorhandler(413)
def file_too_large(error):
    """Handle file upload size limit exceeded"""
    flash('File too large. Maximum file size is 100MB.', 'error')
    return redirect(request.referrer or url_for('dashboard'))

@app.errorhandler(404)
def not_found(error):
    """Enhanced 404 handler"""
    flash('Page not found.', 'error')
    return redirect(url_for('dashboard'))

@app.errorhandler(500) 
def internal_error(error):
    """Enhanced 500 handler"""
    print(f"Internal server error: {error}")
    flash('Something went wrong. Please try again.', 'error')
    return redirect(url_for('dashboard'))

# Add context processor for theme support
@app.context_processor
def inject_theme_info():
    """Inject theme information into all templates"""
    user_theme = 'light'  # default
    
    if 'user' in session and IS_DB_AVAILABLE:
        try:
            user_data = mongo.db.users.find_one({'email': session['user']['email']})
            if user_data and 'theme' in user_data:
                user_theme = user_data['theme']
        except:
            pass
    
    return {
        'user_theme': user_theme,
        'active_users_count': len(active_users),
        'db_available': IS_DB_AVAILABLE,
        'app_version': '2.2.0'
    }

def cleanup_old_files():
    """Clean up old uploaded files (run periodically)"""
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        return
    
    cutoff_time = datetime.now() - timedelta(days=30)  # Remove files older than 30 days
    
    try:
        for filename in os.listdir(app.config['UPLOAD_FOLDER']):
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            if os.path.isfile(file_path):
                file_time = datetime.fromtimestamp(os.path.getctime(file_path))
                if file_time < cutoff_time:
                    try:
                        os.remove(file_path)
                        print(f"Cleaned up old file: {filename}")
                    except Exception as e:
                        print(f"Failed to remove old file {filename}: {e}")
    except Exception as e:
        print(f"Cleanup error: {e}")

# Update the existing background cleanup function
import threading
import time

def enhanced_background_cleanup():
    """Enhanced background thread for cleanup tasks"""
    while True:
        time.sleep(3600)  # Run every hour
        cleanup_inactive_sessions()
        
        # Run file cleanup once per day
        if datetime.now().hour == 2:  # Run at 2 AM
            cleanup_old_files()

# Restart the cleanup thread with enhanced version
if 'cleanup_thread' in locals():
    cleanup_thread = None

cleanup_thread = threading.Thread(target=enhanced_background_cleanup, daemon=True)
cleanup_thread.start()

print("🔧 Backend fixes applied!")
print("📁 File upload/download: Fixed")  
print("🎤 Voice message playback: Fixed")
print("🎨 Theme persistence: Fixed") 
print("🌙 Dark mode visibility: Fixed")


@app.route('/profile')
@login_required
def profile():
    """User profile page"""
    if not IS_DB_AVAILABLE:
        return render_template('profile.html', 
                             user=session['user'], 
                             userdata={}, 
                             friends=[], 
                             friend_requests=[], 
                             message_count=0)
    
    user = mongo.db.users.find_one({'email': session['user']['email']})
    friends = get_user_friends(session['user']['email'])
    friend_requests = get_friend_requests(session['user']['email'])
    
    # Get message count for this user
    message_count = mongo.db.messages.count_documents({
        'author_email': session['user']['email']
    })
    
    return render_template('profile.html', 
                         user=session['user'], 
                         userdata=user,
                         friends=friends, 
                         friend_requests=friend_requests,
                         message_count=message_count)

@app.route('/settings', methods=['GET', 'POST'])
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
                mongo.db.users.update_one(
                    {'email': session['user']['email']},
                    {'$set': {'password': generate_password_hash(new_password)}}
                )
                flash('Password updated successfully!', 'success')
        else:
            # Update profile settings
            updates = {}
            
            if 'username' in request.form:
                new_username = request.form.get('username').strip()
                if new_username and new_username != user['username']:
                    # Check if username is taken
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
                mongo.db.users.update_one(
                    {'email': session['user']['email']},
                    {'$set': updates}
                )
                flash('Settings updated successfully!', 'success')
        
        return redirect(url_for('settings'))
    
    return render_template('settings.html', user=session['user'], userdata=user)

@app.route('/uploads/<path:filename>')
def serve_file(filename):
    """Serve uploaded files with proper MIME types"""
    try:
        # Security: Prevent directory traversal
        if '..' in filename or filename.startswith('/'):
            return "Access denied", 403
        
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        # Check if file exists
        if not os.path.exists(file_path):
            print(f"File not found: {file_path}")
            return "File not found", 404
        
        # Get MIME type
        mime_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
        
        print(f"✅ Serving file: {filename} (MIME: {mime_type})")
        
        # Serve file with proper MIME type
        return send_file(
            file_path,
            mimetype=mime_type,
            as_attachment=False,
            download_name=filename
        )
            
    except Exception as e:
        print(f"❌ Error serving file {filename}: {e}")
        return "Server error" , 500

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
        # Check if already friends
        existing_friendship = mongo.db.friends.find_one({
            '$or': [
                {'user1': session['user']['email'], 'user2': recipient['email']},
                {'user1': recipient['email'], 'user2': session['user']['email']}
            ]
        })
        
        if existing_friendship:
            flash('You are already friends with this user.', 'error')
        else:
            # Check if request already exists
            existing_request = mongo.db.friend_requests.find_one({
                'sender': session['user']['email'],
                'recipient': recipient['email'],
                'status': 'pending'
            })
            
            if existing_request:
                flash('Friend request already sent.', 'error')
            else:
                mongo.db.friend_requests.insert_one({
                    'sender': session['user']['email'],
                    'recipient': recipient['email'],
                    'status': 'pending',
                    'sent_at': datetime.now(timezone.utc)
                })
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
    
    # Remove the friend request
    mongo.db.friend_requests.delete_one({
        'sender': sender_email,
        'recipient': recipient_email
    })
    
    # Create friendship
    room = get_private_room_id(sender_email, recipient_email)
    mongo.db.friends.insert_one({
        'user1': sender_email,
        'user2': recipient_email,
        'room': room,
        'created_at': datetime.now(timezone.utc)
    })
    
    # Get sender's username for flash message
    sender = mongo.db.users.find_one({'email': sender_email})
    sender_name = sender['username'] if sender else 'Unknown user'
    
    flash(f'You are now friends with {sender_name}!', 'success')
    return redirect(url_for('dashboard'))

# File upload routes
@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    """Handle file uploads with proper path management"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'No file selected'})
    
    file = request.files['file']
    room = request.form.get('room')
    
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No file selected'})
    
    if not room:
        return jsonify({'success': False, 'message': 'Room required'})
    
    if file and allowed_file(file.filename):
        # Generate unique filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = secure_filename(file.filename)
        unique_filename = f"{timestamp}_{filename}"
        
        # Ensure upload directory exists
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        
        try:
            file.save(file_path)
            
            # Get file info
            file_size = os.path.getsize(file_path)
            file_type = get_file_type(filename)
            mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            
            # CRITICAL: Use URL paths for serving
            file_url = f"/uploads/{unique_filename}"
            
            # Create thumbnail if image
            thumbnail_url = None
            if file_type == 'image':
                thumbnail_url = create_thumbnail(file_path, unique_filename)
                if thumbnail_url:
                    thumbnail_url = f"/{thumbnail_url}"  # Add leading slash
            
            # Save file info to database
            file_data = {
                'room': room,
                'author_username': session['user']['username'],
                'author_email': session['user']['email'],
                'message_type': 'file',
                'file_info': {
                    'original_name': filename,
                    'filename': unique_filename,
                    'file_path': file_url,  # Use URL path
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
            
            print(f"📎 File uploaded: {filename} -> {file_url}")
            
            # Emit to room via SocketIO
            socketio.emit('new_message', {
                'author_username': file_data['author_username'],
                'text': file_data['text'],
                'timestamp': file_data['timestamp'].isoformat(),
                'message_type': 'file',
                'file_info': file_data['file_info']
            }, room=room)
            
            return jsonify({
                'success': True, 
                'message': 'File uploaded successfully',
                'file_info': file_data['file_info']
            })
            
        except Exception as e:
            print(f"❌ File upload error: {e}")
            return jsonify({'success': False, 'message': f'Upload failed: {str(e)}'})
    
    return jsonify({'success': False, 'message': 'File type not allowed'})

@app.route('/download/<filename>')
@login_required
def download_file(filename):
    """Secure file download"""
    try:
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)
    except FileNotFoundError:
        flash('File not found', 'error')
        return redirect(url_for('dashboard'))

@app.route('/upload_voice', methods=['POST'])
@login_required
def upload_voice_message():
    """Handle voice message uploads with proper paths"""
    if 'audio' not in request.files:
        return jsonify({'success': False, 'message': 'No audio file'})
    
    audio_file = request.files['audio']
    room = request.form.get('room')
    duration = float(request.form.get('duration', '0'))
    
    if not room:
        return jsonify({'success': False, 'message': 'Room required'})
    
    try:
        # Generate unique filename for voice message
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_username = secure_filename(session['user']['username'])
        filename = f"voice_{timestamp}_{safe_username}.webm"
        
        # Ensure upload directory exists
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        audio_file.save(file_path)
        file_size = os.path.getsize(file_path)
        
        # CRITICAL: Use correct URL path for playback
        file_url = f"/uploads/{filename}"
        
        # Save voice message to database
        voice_data = {
            'room': room,
            'author_username': session['user']['username'],
            'author_email': session['user']['email'],
            'message_type': 'voice',
            'voice_info': {
                'filename': filename,
                'file_path': file_url,  # Use URL path, not filesystem path
                'duration': duration,
                'file_size': file_size
            },
            'text': f"🎤 Voice message ({duration:.1f}s)",
            'timestamp': datetime.now(timezone.utc)
        }
        
        if IS_DB_AVAILABLE:
            result = mongo.db.messages.insert_one(voice_data)
            voice_data['_id'] = result.inserted_id
        
        print(f"🎤 Voice message saved: {filename} -> {file_url}")
        
        # Emit to room
        socketio.emit('new_message', {
            'author_username': voice_data['author_username'],
            'text': voice_data['text'],
            'timestamp': voice_data['timestamp'].isoformat(),
            'message_type': 'voice',
            'voice_info': voice_data['voice_info']
        }, room=room)
        
        return jsonify({
            'success': True,
            'message': 'Voice message sent',
            'voice_info': voice_data['voice_info']
        })
        
    except Exception as e:
        print(f"❌ Voice upload error: {e}")
        return jsonify({'success': False, 'message': f'Voice upload failed: {str(e)}'})

    
# SocketIO Events
@socketio.on('connect')
def on_connect():
    if 'user' in session:
        user_info = {
            'email': session['user']['email'],
            'username': session['user']['username'],
            'connected_at': datetime.now(timezone.utc)
        }
        active_users[request.sid] = user_info
        
        print(f"✅ {session['user']['username']} connected (SID: {request.sid})")
        
        # Notify friends that user came online
        emit('user_status_change', {
            'email': session['user']['email'],
            'username': session['user']['username'],
            'status': 'online'
        }, broadcast=True)

@socketio.on('disconnect')
def on_disconnect():
    if request.sid in active_users:
        user_info = active_users[request.sid]
        print(f"❌ {user_info['username']} disconnected")
        
        # Clean up typing status
        for room in list(typing_users.keys()):
            if request.sid in typing_users[room]:
                typing_users[room].discard(request.sid)
                if not typing_users[room]:
                    del typing_users[room]
        
        # Notify friends that user went offline
        emit('user_status_change', {
            'email': user_info['email'],
            'username': user_info['username'],
            'status': 'offline'
        }, broadcast=True)
        
        del active_users[request.sid]

@socketio.on('join_room')
def on_join_room(data):
    room = data.get('room')
    if room and 'user' in session:
        join_room(room)
        user_rooms[request.sid] = room
        
        print(f"👥 {session['user']['username']} joined room: {room}")
        
        # Mark messages as read
        if IS_DB_AVAILABLE:
            mark_messages_as_read(session['user']['email'], room)
        
        # Notify room that user joined
        emit('user_joined', {
            'username': session['user']['username'],
            'room': room
        }, to=room, include_self=False)

@socketio.on('leave_room')
def on_leave_room(data):
    room = data.get('room')
    if room and request.sid in user_rooms:
        leave_room(room)
        if request.sid in user_rooms:
            del user_rooms[request.sid]
        
        print(f"👋 {session['user']['username']} left room: {room}")
        
        # Notify room that user left
        emit('user_left', {
            'username': session['user']['username'],
            'room': room
        }, to=room)

@socketio.on('send_message')
def on_send_message(data):
    if 'user' not in session:
        return
    
    message_text = data.get('message', '').strip()
    room = data.get('room', '')
    
    if not message_text or not room:
        return
    
    # Create message object
    message_data = {
        'room': room,
        'author_username': session['user']['username'],
        'author_email': session['user']['email'],
        'text': message_text,
        'timestamp': datetime.now(timezone.utc),
        'message_type': 'text'
    }
    
    # Save to database
    if IS_DB_AVAILABLE:
        result = mongo.db.messages.insert_one(message_data)
        message_data['_id'] = result.inserted_id
    
    print(f"💬 Message from {session['user']['username']} in {room}: {message_text[:50]}...")
    
    # Broadcast message to room
    emit('new_message', {
        'author_username': message_data['author_username'],
        'text': message_data['text'],
        'timestamp': message_data['timestamp'].isoformat(),
        'message_type': 'text'
    }, to=room)

@socketio.on('typing')
def on_typing(data):
    if 'user' not in session:
        return
    
    room = data.get('room')
    if room:
        # Add user to typing list for this room
        if room not in typing_users:
            typing_users[room] = set()
        typing_users[room].add(request.sid)
        
        # Notify others in room
        emit('user_typing', {
            'username': session['user']['username'],
            'room': room
        }, to=room, include_self=False)

@socketio.on('stop_typing')
def on_stop_typing(data):
    if 'user' not in session:
        return
    
    room = data.get('room')
    if room and room in typing_users:
        typing_users[room].discard(request.sid)
        
        # Clean up empty typing rooms
        if not typing_users[room]:
            del typing_users[room]
        
        # Notify others in room
        emit('user_stopped_typing', {
            'username': session['user']['username'],
            'room': room
        }, to=room, include_self=False)

@socketio.on('get_online_users')
def on_get_online_users():
    """Get list of currently online users"""
    online_users = []
    for user_data in active_users.values():
        online_users.append({
            'username': user_data['username'],
            'email': user_data['email']
        })
    
    emit('online_users', {'users': online_users})

# Error handlers
@app.errorhandler(404)
def not_found(error):
    flash('Page not found.', 'error')
    return redirect(url_for('dashboard'))

@app.errorhandler(500)
def internal_error(error):
    flash('Something went wrong. Please try again.', 'error')
    return redirect(url_for('dashboard'))

# Template filters
@app.template_filter('timeago')
def timeago_filter(dt):
    if not dt:
        return "Unknown"
    
    now = datetime.now(timezone.utc)
    # Convert naive datetime to aware datetime in UTC
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
    """Format datetime for display"""
    if not dt:
        return 'Now'
    
    # Handle string timestamps
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return dt  # Return as-is if can't parse
    
    try:
        return dt.strftime('%I:%M %p')
    except AttributeError:
        return str(dt)

@app.template_filter('file_size')
def file_size_filter(size_bytes):
    """Convert file size to human readable format"""
    if not size_bytes:
        return '0 B'
    
    try:
        size_bytes = float(size_bytes)
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024:
                return f'{size_bytes:.1f} {unit}'
            size_bytes /= 1024
        return f'{size_bytes:.1f} TB'
    except (ValueError, TypeError):
        return '0 B'


def get_file_type(filename):
    """Determine file type category with better detection"""
    if not filename or '.' not in filename:
        return 'file'
    
    ext = filename.rsplit('.', 1)[1].lower()
    
    image_types = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg']
    video_types = ['mp4', 'avi', 'mov', 'wmv', 'mkv', 'webm', 'flv']
    audio_types = ['mp3', 'wav', 'ogg', 'm4a', 'aac', 'flac']
    document_types = ['pdf', 'doc', 'docx', 'txt', 'rtf', 'odt']
    
    if ext in image_types:
        return 'image'
    elif ext in video_types:
        return 'video'  
    elif ext in audio_types:
        return 'audio'
    elif ext in document_types:
        return 'document'
    else:
        return 'file'

# Background cleanup
def cleanup_inactive_sessions():
    """Clean up inactive user sessions"""
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
    
    inactive_sids = []
    for sid, user_data in active_users.items():
        connected_at = user_data.get('connected_at', datetime.now(timezone.utc))
        if connected_at < cutoff_time:
            inactive_sids.append(sid)
    
    for sid in inactive_sids:
        if sid in active_users:
            del active_users[sid]
        if sid in user_rooms:
            del user_rooms[sid]

# Context processor
@app.context_processor
def inject_debug_info():
    """Inject debug information into templates"""
    return {
        'active_users_count': len(active_users),
        'db_available': IS_DB_AVAILABLE,
        'app_version': '2.1.0'
    }

if __name__ == '__main__':
    print("🚀 Starting Enhanced Messenger App...")
    print(f"📊 Database: {'Connected' if IS_DB_AVAILABLE else 'Disabled'}")
    print(f"👥 Active users tracking: Enabled")
    print(f"💬 Real-time messaging: Enabled")
    print(f"📝 Typing indicators: Enabled")
    print(f"📎 File sharing: Enabled")
    print(f"🎤 Voice messages: Enabled")
    print("=" * 50)
    
    socketio.run(
        app, 
        debug=True, 
        host='127.0.0.1', 
        port=5000
    )
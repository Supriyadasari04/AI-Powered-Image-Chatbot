import os
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import google.generativeai as genai
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image
import io
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///db.sqlite'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif'}
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB limit

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Configure Gemini
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-1.5-flash')

# Database Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True)
    password = db.Column(db.String(100))
    name = db.Column(db.String(100))
    chats = db.relationship('Chat', backref='user', lazy=True)

class Chat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200))
    image_path = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    messages = db.relationship('Message', backref='chat', lazy=True, cascade='all, delete-orphan')

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text)
    role = db.Column(db.String(20))  # 'user' or 'assistant'
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    chat_id = db.Column(db.Integer, db.ForeignKey('chat.id'))

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Helper Functions
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def process_image(image_path):
    try:
        img = Image.open(image_path)
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr = img_byte_arr.getvalue()
        
        response = model.generate_content([
            "Describe this image in detail, including objects, colors, text, and context.",
            {"mime_type": "image/png", "data": img_byte_arr}
        ])
        
        return response.text
    except Exception as e:
        return f"Error processing image: {str(e)}"

# Routes
@app.route('/')
@login_required
def home():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('home'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()

        if not user or not check_password_hash(user.password, password):
            flash('Invalid credentials')
            return redirect(url_for('login'))

        login_user(user)
        return redirect(url_for('home'))

    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('home'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        name = request.form.get('name')

        if password != confirm_password:
            flash('Passwords do not match')
            return redirect(url_for('signup'))

        if len(password) < 8:
            flash('Password must be at least 8 characters')
            return redirect(url_for('signup'))

        if not any(char.isdigit() for char in password):
            flash('Password must contain at least one number')
            return redirect(url_for('signup'))

        if not any(char.isupper() for char in password):
            flash('Password must contain at least one uppercase letter')
            return redirect(url_for('signup'))
        
        if User.query.filter_by(username=username).first():
            flash('Username already exists')
            return redirect(url_for('signup'))

        new_user = User(
            username=username,
            password=generate_password_hash(password),
            name=name
        )
        db.session.add(new_user)
        db.session.commit()

        login_user(new_user)
        return redirect(url_for('home'))

    return render_template('signup.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    
    if not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type"}), 400
    
    try:
        filename = secure_filename(file.filename)
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(save_path)
        
        description = process_image(save_path)
        
        chat = Chat(
            title=f"Chat {datetime.now().strftime('%m/%d %H:%M')}",
            image_path=f"/static/uploads/{filename}",
            user_id=current_user.id
        )
        db.session.add(chat)
        
        message = Message(
            content=description,
            role='assistant',
            chat=chat
        )
        db.session.add(message)
        db.session.commit()
        
        return jsonify({
            "chat_id": chat.id,
            "image_url": chat.image_path,
            "description": description
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/ask', methods=['POST'])
@login_required
def ask_question():
    data = request.json
    chat_id = data.get('chat_id')
    question = data.get('question')
    
    if not chat_id or not question:
        return jsonify({"error": "Missing chat_id or question"}), 400
    
    chat = Chat.query.filter_by(id=chat_id, user_id=current_user.id).first()
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    
    try:
        response = model.generate_content([
            "Context from previous messages:",
            *[f"{msg.role}: {msg.content}" for msg in chat.messages],
            f"User question: {question}",
            {"mime_type": "image/png", "data": open(chat.image_path[1:], 'rb').read()}
        ])
        
        answer = response.text
        
        user_msg = Message(content=question, role='user', chat=chat)
        bot_msg = Message(content=answer, role='assistant', chat=chat)
        db.session.add_all([user_msg, bot_msg])
        db.session.commit()
        
        return jsonify({"answer": answer})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/chats')
@login_required
def get_chats():
    chats = Chat.query.filter_by(user_id=current_user.id).order_by(Chat.created_at.desc()).all()
    return jsonify([{
        "id": chat.id,
        "title": chat.title,
        "image_url": chat.image_path,
        "created_at": chat.created_at.isoformat()
    } for chat in chats])

@app.route('/chat/<int:chat_id>')
@login_required
def get_chat(chat_id):
    chat = Chat.query.filter_by(id=chat_id, user_id=current_user.id).first()
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    
    return jsonify({
        "id": chat.id,
        "title": chat.title,
        "image_url": chat.image_path,
        "messages": [{
            "role": msg.role,
            "content": msg.content,
            "timestamp": msg.timestamp.isoformat()
        } for msg in chat.messages]
    })

@app.route('/chat/<int:chat_id>/rename', methods=['POST'])
@login_required
def rename_chat(chat_id):
    data = request.json
    new_title = data.get('title')
    
    if not new_title:
        return jsonify({"error": "New title is required"}), 400
    
    chat = Chat.query.filter_by(id=chat_id, user_id=current_user.id).first()
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    
    chat.title = new_title
    db.session.commit()
    
    return jsonify({"success": True, "new_title": new_title})

@app.route('/chat/<int:chat_id>', methods=['DELETE'])
@login_required
def delete_chat(chat_id):
    chat = Chat.query.filter_by(id=chat_id, user_id=current_user.id).first()
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    
    try:
        if chat.image_path:
            image_path = chat.image_path[1:]  
            if os.path.exists(image_path):
                os.remove(image_path)
    except Exception as e:
        print(f"Error deleting image file: {e}")
    
    db.session.delete(chat)
    db.session.commit()
    
    return jsonify({"success": True})

@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html')

@app.route('/profile/update', methods=['POST'])
@login_required
def update_profile():
    data = request.json
    new_name = data.get('name')
    new_username = data.get('username')
    
    if not new_name or not new_username:
        return jsonify({"error": "Name and username are required"}), 400
    
    if new_username != current_user.username and User.query.filter_by(username=new_username).first():
        return jsonify({"error": "Username already taken"}), 400
    
    current_user.name = new_name
    current_user.username = new_username
    db.session.commit()
    
    return jsonify({
        "success": True,
        "name": current_user.name,
        "username": current_user.username
    })

@app.route('/profile/delete', methods=['DELETE'])
@login_required
def delete_profile():
    try:
        chats = Chat.query.filter_by(user_id=current_user.id).all()
        for chat in chats:
            if chat.image_path:
                try:
                    image_path = chat.image_path[1:] 
                    if os.path.exists(image_path):
                        os.remove(image_path)
                except Exception as e:
                    print(f"Error deleting image: {e}")
        
        db.session.delete(current_user)
        db.session.commit()
        
        logout_user()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/profile/change-password', methods=['POST'])
@login_required
def change_password():
    data = request.json
    current_password = data.get('current_password')
    new_password = data.get('new_password')
    confirm_password = data.get('confirm_password')
    
    if not current_password or not new_password or not confirm_password:
        return jsonify({"error": "All fields are required"}), 400
    
    if new_password != confirm_password:
        return jsonify({"error": "New passwords don't match"}), 400
    
    if not check_password_hash(current_user.password, current_password):
        return jsonify({"error": "Current password is incorrect"}), 400
    
    if check_password_hash(current_user.password, new_password):
        return jsonify({"error": "New password must be different"}), 400
    
    current_user.password = generate_password_hash(new_password)
    db.session.commit()
    
    return jsonify({"success": True})

# Initialize database
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    app.run(debug=True)
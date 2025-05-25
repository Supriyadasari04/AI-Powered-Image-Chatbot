from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import google.generativeai as genai
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image
import os
from dotenv import load_dotenv
import io
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///db.sqlite'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif'}
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB limit

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Configure Gemini
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-1.5-flash')

# Conversation context
conversation_history = []

# User loader
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

from flask_login import UserMixin

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    username = db.Column(db.String(100), unique=True)
    password = db.Column(db.String(100))

    # These are required by Flask-Login
    @property
    def is_active(self):
        return True  # All users are active in this implementation

    @property
    def is_authenticated(self):
        return True  # Return True if user is authenticated

    @property
    def is_anonymous(self):
        return False  # Return False for regular users

    def get_id(self):
        return str(self.id)  # Return the user ID as unicode

    def __repr__(self):
        return f'<User {self.username}>'



# Helper functions
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def process_image(image_path):
    try:
        img = Image.open(image_path)
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr = img_byte_arr.getvalue()
        
        response = model.generate_content(
            [
                "Describe this image in detail, including objects, colors, text, and context.",
                {"mime_type": "image/png", "data": img_byte_arr}
            ],
            request_options={'timeout': 10}
        )
        
        if not response or not hasattr(response, 'text'):
            return {"error": "Invalid response from Gemini API"}
        
        return {
            "description": response.text,
            "image_path": image_path
        }
        
    except Exception as e:
        return {"error": f"API error: {str(e)}"}

# Routes
@app.route('/')
@login_required
def home():
    return render_template('index.html', name=current_user.name)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = True if request.form.get('remember') else False

        user = User.query.filter_by(username=username).first()

        if not user or not check_password_hash(user.password, password):
            flash('Invalid username or password')
            return redirect(url_for('login'))

        login_user(user, remember=remember)
        return redirect(url_for('home'))
    
    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    
    if request.method == 'POST':
        name = request.form.get('name')
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        # Password validation
        if len(password) < 8:
            flash('Password must be at least 8 characters')
            return redirect(url_for('signup'))
        if not any(char.isdigit() for char in password):
            flash('Password must contain at least one number')
            return redirect(url_for('signup'))
        if not any(char.isupper() for char in password):
            flash('Password must contain at least one uppercase letter')
            return redirect(url_for('signup'))
        if password != confirm_password:
            flash('Passwords do not match')
            return redirect(url_for('signup'))

        if User.query.filter_by(username=username).first():
            flash('Username already exists')
            return redirect(url_for('signup'))

        new_user = User(
            name=name,
            username=username,
            password=generate_password_hash(password, method='pbkdf2:sha256')
        )

        db.session.add(new_user)
        db.session.commit()

        flash('Account created successfully! Please log in.')
        return redirect(url_for('login'))
    
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
    
    if file and allowed_file(file.filename):
        try:
            filename = secure_filename(file.filename)
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(save_path)
            
            result = process_image(save_path)
            
            if 'error' in result:
                return jsonify({"error": result['error']}), 500
                
            if 'description' not in result:
                return jsonify({"error": "No description generated"}), 500
            
            global conversation_history
            conversation_history = [
                {"role": "assistant", "content": result['description']}
            ]
            
            return jsonify({
                "message": "Image uploaded successfully",
                "description": result['description'],
                "image_url": f"/static/uploads/{filename}"
            })
            
        except Exception as e:
            return jsonify({"error": f"Server error: {str(e)}"}), 500
    
    return jsonify({"error": "Invalid file type"}), 400

@app.route('/ask', methods=['POST'])
@login_required
def ask_question():
    data = request.json
    question = data.get('question')
    image_path = data.get('image_path')
    
    if not question or not image_path:
        return jsonify({"error": "Missing question or image path"}), 400
    
    try:
        full_path = os.path.join('static/uploads', os.path.basename(image_path))
        img = Image.open(full_path)
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_data = img_byte_arr.getvalue()
        
        response = model.generate_content(
            [
                "Here's our conversation so far:\n" + 
                "\n".join([f"{msg['role']}: {msg['content']}" for msg in conversation_history[-3:]]),
                question,
                {"mime_type": "image/png", "data": img_data}
            ],
            request_options={'timeout': 10}
        )
        
        answer = response.text
        conversation_history.append({"role": "user", "content": question})
        conversation_history.append({"role": "assistant", "content": answer})
        
        return jsonify({
            "status": "success",
            "answer": answer,
            "conversation": conversation_history
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Initialize database
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
    app.run(debug=True)
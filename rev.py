import os
import json
import uuid
from datetime import datetime
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from pymongo import MongoClient
from bson import ObjectId
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from werkzeug.security import generate_password_hash, check_password_hash
from bson.json_util import dumps

def serialize_session(session):
    """Helper function to serialize session data"""
    if session:
        session = dict(session)  # Convert from MongoDB document if needed
        session['_id'] = str(session['_id'])
        session['user_id'] = str(session['user_id'])
    return session

class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

app = Flask(__name__)

# Configure JSON encoder properly
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
app.json_encoder = CustomJSONEncoder

CORS(app, supports_credentials=True)

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")
URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={API_KEY}"
headers = {"Content-Type": "application/json"}
SESSIONS_DIR = "chat_sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'your-secret-key')
app.config['JWT_TOKEN_LOCATION'] = ['headers']
jwt = JWTManager(app)

# MongoDB connection
client = MongoClient('mongodb+srv://ReeVNaR:Ranveer1516@cluster0.zqv7j.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0')
db = client['revi_chat']

def update_user_memory(user_id, message, response):
    """Update user's persistent memory with important information"""
    memory = db.user_memory.find_one({'user_id': ObjectId(user_id)}) or {
        'user_id': ObjectId(user_id),
        'memories': [],
        'created_at': datetime.utcnow()
    }
    
    # Add new memory if important information is detected
    memory['memories'].append({
        'content': f"User: {message}\nRevi: {response}",
        'timestamp': datetime.utcnow()
    })
    
    # Keep only last 10 important memories
    memory['memories'] = memory['memories'][-10:]
    
    # Upsert the memory document
    db.user_memory.update_one(
        {'user_id': ObjectId(user_id)},
        {'$set': memory},
        upsert=True
    )

def get_user_context(user_id, username):
    """Get user's context including memories"""
    memory = db.user_memory.find_one({'user_id': ObjectId(user_id)})
    memories = memory.get('memories', []) if memory else []
    
    memory_text = "\n".join([m['content'] for m in memories[-3:]])  # Last 3 memories
    
    return {
        "role": "user",
        "parts": [{
            "text": f"Important context from previous conversations:\n{memory_text}\n\nNow let's continue our chat."
        }]
    }

@app.route('/auth/register', methods=['POST'])
def register():
    data = request.json
    if not data.get('username') or not data.get('password'):
        return jsonify({'error': 'Username and password are required'}), 400
        
    if len(data['password']) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    
    if db.users.find_one({'username': data['username']}):
        return jsonify({'error': 'Username already exists'}), 400
    
    user = {
        'username': data['username'],
        'password': generate_password_hash(data['password']),
        'created_at': datetime.utcnow()
    }
    db.users.insert_one(user)
    return jsonify({'message': 'User created successfully'}), 201

@app.route('/auth/login', methods=['POST'])
def login():
    data = request.json
    user = db.users.find_one({'username': data['username']})
    
    if user and check_password_hash(user['password'], data['password']):
        access_token = create_access_token(identity=str(user['_id']))
        return jsonify({'token': access_token, 'username': user['username']}), 200
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/auth/login')
def login_page():
    return render_template('auth.html')

@app.route('/sessions', methods=['GET'])
@jwt_required()
def list_sessions():
    user_id = get_jwt_identity()
    # Get user info
    user = db.users.find_one({'_id': ObjectId(user_id)})
    if not user:
        return jsonify({'error': 'User not found'}), 404
        
    # Get only this user's sessions
    sessions = list(db.sessions.find({
        'user_id': ObjectId(user_id)
    }).sort('created_at', -1))
    
    return jsonify([serialize_session(s) for s in sessions])

@app.route('/sessions', methods=['POST'])
@jwt_required()
def create_session():
    user_id = get_jwt_identity()
    # Get user info
    user = db.users.find_one({'_id': ObjectId(user_id)})
    if not user:
        return jsonify({'error': 'User not found'}), 404
        
    session = {
        'user_id': ObjectId(user_id),
        'username': user['username'],  # Store username for easier reference
        'title': 'New Chat',
        'messages': [],
        'created_at': datetime.utcnow(),
        'last_updated': datetime.utcnow()
    }
    result = db.sessions.insert_one(session)
    session['_id'] = result.inserted_id
    return jsonify(serialize_session(session))

@app.route('/sessions/<session_id>', methods=['GET'])
@jwt_required()
def get_session(session_id):
    user_id = get_jwt_identity()
    session = db.sessions.find_one({'_id': ObjectId(session_id), 'user_id': ObjectId(user_id)})
    if session:
        return jsonify(serialize_session(session))
    return jsonify({'error': 'Session not found'}), 404

@app.route('/sessions/<session_id>', methods=['DELETE'])
@jwt_required()
def delete_session(session_id):
    user_id = get_jwt_identity()
    result = db.sessions.delete_one({'_id': ObjectId(session_id), 'user_id': ObjectId(user_id)})
    if result.deleted_count:
        return jsonify({"status": "success"})
    return jsonify({"error": "Session not found"}), 404

# Add user-specific system prompt
def get_user_context_message(username):
    return {
        "role": "model",  # Changed from "system" to "model"
        "parts": [{"text": f"I am Revi, your AI assistant. I will maintain context for our conversation."}]
    }

@app.route('/sessions/<session_id>/chat', methods=['POST'])
@jwt_required()
def session_chat(session_id):
    user_id = get_jwt_identity()
    user = db.users.find_one({'_id': ObjectId(user_id)})
    if not user:
        return jsonify({'error': 'User not found'}), 404
        
    data = request.json
    user_message = data.get('message')
    
    if not user_message:
        return jsonify({"error": "No message provided"}), 400
    
    # Add username verification to session lookup
    session = db.sessions.find_one({
        '_id': ObjectId(session_id),
        'user_id': ObjectId(user_id),
        'username': user['username']  # Extra verification
    })
    
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    
    # Initialize messages with user context
    if not session.get("messages"):
        session["messages"] = [get_user_context(user_id, user['username'])]
        session["title"] = user_message[:30] + "..." if len(user_message) > 30 else user_message
    
    # Add the context message if this is a new conversation
    if not session["messages"]:
        context_message = get_user_context_message(user['username'])
        session["messages"].append(context_message)
    
    # Keep only recent context (last 5 messages)
    recent_messages = session["messages"][-4:] if session["messages"] else []
    
    # Add user message
    session["messages"].append({
        "role": "user",
        "parts": [{"text": user_message}]
    })
    
    # Prepare request with recent context
    body = {
        "contents": recent_messages + [session["messages"][-1]],
        "generationConfig": {
            "temperature": 0.7,
            "topK": 40,
            "topP": 0.95,
            "maxOutputTokens": 1024,
        }
    }
    
    try:
        response = requests.post(URL, headers=headers, json=body)
        data = response.json()
        
        if "error" in data:
            return jsonify({"error": data["error"]["message"]}), 500
            
        reply_text = data["candidates"][0]["content"]["parts"][0]["text"]
        
        # Update user's memory
        update_user_memory(user_id, user_message, reply_text)
        
        # Add assistant's reply
        session["messages"].append({
            "role": "model",
            "parts": [{"text": reply_text}]
        })
        
        # Save updated session with context
        db.sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {
                'messages': session["messages"],
                'title': session["title"],
                'last_updated': datetime.utcnow()
            }}
        )
        
        return jsonify({
            "response": reply_text,
            "session": serialize_session(session)
        })
        
    except Exception as e:
        return jsonify({"error": f"Chat error: {str(e)}"}), 500

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                             'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Route not found"}), 404

@app.errorhandler(Exception)
def handle_error(error):
    if hasattr(error, 'data'):
        return jsonify(error.data), error.code
    return jsonify({"error": str(error)}), 500

@app.route('/history', methods=['GET'])
@jwt_required()
def get_history():
    user_id = get_jwt_identity()
    sessions = list(db.sessions.find({'user_id': ObjectId(user_id)}).sort('created_at', -1))
    for session in sessions:
        session['_id'] = str(session['_id'])
    return jsonify(sessions)

@app.route('/history/clear', methods=['POST'])
@jwt_required()
def clear_history():
    user_id = get_jwt_identity()
    db.sessions.delete_many({'user_id': ObjectId(user_id)})
    return jsonify({"status": "success"})

if __name__ == "__main__":
    app.run(debug=True, port=5000)

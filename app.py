import os
import json
import uuid
import logging
import threading
from dotenv import load_dotenv

load_dotenv()  # must be first, before any module that reads env vars at import time

from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate

from models import db, User, Video, Quiz
from main import generate_video
from quiz import generate_quiz
from progress import set_progress, get_progress

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ── Flask app ──────────────────────────────────────────────────────────────────

FRONTEND_BUILD = os.path.join(os.getcwd(), "frontend", "build")
app = Flask(__name__, static_folder=FRONTEND_BUILD, template_folder=FRONTEND_BUILD)

SECRET_KEY = os.getenv("SECRET_KEY", "")
if SECRET_KEY == "your-secret-key-change-me" or not SECRET_KEY:
    raise RuntimeError("Set a real SECRET_KEY in your .env before running")

app.config["SECRET_KEY"]                  = SECRET_KEY
app.config["SQLALCHEMY_DATABASE_URI"]     = os.getenv("DATABASE_URL", "sqlite:///site.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"]              = os.path.join(os.getcwd(), "uploads")

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(os.path.join(os.getcwd(), "output"), exist_ok=True)

db.init_app(app)
Migrate(app, db)

# CORS — restrict to known frontend origins
_allowed_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")
CORS(app, resources={r"/*": {"origins": _allowed_origins}}, supports_credentials=True)


# ── Rate limiter ───────────────────────────────────────────────────────────────

def _rate_limit_key():
    if current_user.is_authenticated:
        return f"user:{current_user.id}"
    return get_remote_address()

limiter = Limiter(app=app, key_func=_rate_limit_key, default_limits=[], storage_uri=os.getenv("REDIS_URL"))


# ── Flask-Login ────────────────────────────────────────────────────────────────

login_manager = LoginManager(app)
login_manager.login_view = "login"

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


with app.app_context():
    db.create_all()


# ── Frontend ───────────────────────────────────────────────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_react(path):
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, "index.html")


# ── Auth ───────────────────────────────────────────────────────────────────────

@app.route("/signup", methods=["POST"])
@limiter.limit("10 per minute")
def signup():
    data     = request.json or request.form
    username = data.get("username")
    password = data.get("password")
    email    = data.get("email")

    if not username or not password or not email:
        return jsonify({"error": "Username, email, and password are required."}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({"error": "Username already exists."}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Email already exists."}), 400

    new_user = User(username=username, email=email, password=generate_password_hash(password))
    db.session.add(new_user)
    db.session.commit()
    return jsonify({"success": True, "message": "Signup successful, please log in."}), 200


@app.route("/login", methods=["POST"])
@limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return jsonify({"success": True, "message": "Already logged in."}), 200

    data       = request.json or request.form
    identifier = data.get("username")
    password   = data.get("password")

    user = (
        User.query.filter_by(email=identifier).first()
        if identifier and "@" in identifier
        else User.query.filter_by(username=identifier).first()
    )

    if user and check_password_hash(user.password, password):
        login_user(user)
        return jsonify({"success": True, "message": "Logged in successfully."}), 200
    return jsonify({"error": "Invalid credentials."}), 400


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return jsonify({"success": True, "message": "Logged out successfully."}), 200


# ── Video generation (async) ───────────────────────────────────────────────────

@app.route("/generate-video", methods=["POST"])
@login_required
@limiter.limit("3 per minute")
def generate_video_endpoint():
    prompt = request.form.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Prompt is required."}), 400
    if len(prompt) > 2000:
        return jsonify({"error": "Prompt must be 2000 characters or fewer."}), 400

    # Handle optional file attachment
    attachment_filename = None
    attachment = request.files.get("attachment")
    if attachment:
        filename = secure_filename(attachment.filename)
        folder = (
            os.path.join(app.config["UPLOAD_FOLDER"], "pdf")
            if filename.lower().endswith(".pdf")
            else os.path.join(app.config["UPLOAD_FOLDER"], "images")
        )
        os.makedirs(folder, exist_ok=True)
        attachment.save(os.path.join(folder, filename))
        attachment_filename = filename
        prompt += f" (See attached file: {filename})"

    task_id  = str(uuid.uuid4())
    username = current_user.username
    user_id  = current_user.id

    words            = prompt.split()
    computed_filename = secure_filename(f"{user_id}_{'_'.join(words[:10])}.mp4")
    user_output_folder = os.path.join("output", f"{username}_output")
    os.makedirs(user_output_folder, exist_ok=True)
    output_file_path = os.path.join(user_output_folder, computed_filename)

    # Create a pending DB record immediately so history shows it
    new_video = Video(
        user_id=user_id,
        filename=computed_filename,
        filepath=output_file_path,
        prompt_text=prompt,
        task_id=task_id,
        status="processing",
        attachment_filename=attachment_filename,
    )
    db.session.add(new_video)
    db.session.commit()
    video_db_id = new_video.id

    set_progress({"state": "processing", "step": "Initializing", "message": ""}, user_id=task_id)

    def _run():
        with app.app_context():
            video_rec = db.session.get(Video, video_db_id)
            try:
                success, script = generate_video(prompt, computed_filename, username, task_id=task_id)
                if success and os.path.exists(output_file_path):
                    set_progress(
                        {"state": "completed", "filename": computed_filename, "step": "Completed", "script": script},
                        user_id=task_id,
                    )
                    video_rec.status = "completed"
                else:
                    set_progress({"state": "failed", "step": "Error", "message": "File not found after generation"}, user_id=task_id)
                    video_rec.status = "failed"
            except Exception as e:
                logger.exception("Video generation thread failed for task %s", task_id)
                set_progress({"state": "failed", "step": "Error", "message": str(e)}, user_id=task_id)
                video_rec.status = "failed"
                video_rec.error_message = str(e)
            finally:
                db.session.commit()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id, "video_id": video_db_id}), 202


@app.route("/task-status/<task_id>", methods=["GET"])
@login_required
def task_status(task_id):
    info = get_progress(user_id=task_id)
    return jsonify(info), 200


# ── File serving ───────────────────────────────────────────────────────────────

@app.route("/upload-pdf", methods=["POST"])
@login_required
def upload_pdf():
    if "attachment" not in request.files:
        return jsonify({"error": "No file part in request."}), 400
    file = request.files["attachment"]
    if not file.filename:
        return jsonify({"error": "No file selected."}), 400
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Invalid file type. PDF only."}), 400

    filename    = secure_filename(file.filename)
    user_folder = os.path.join(app.config["UPLOAD_FOLDER"], f"{current_user.username}_file", "pdf")
    os.makedirs(user_folder, exist_ok=True)
    save_path = os.path.join(user_folder, filename)
    file.save(save_path)
    return jsonify({"success": True, "filename": filename, "path": save_path}), 200


@app.route("/download-video", methods=["GET"])
@login_required
def download_video():
    filename = request.args.get("filename", "").strip()
    if not filename:
        return jsonify({"error": "filename query parameter is required."}), 400
    output_path = os.path.join("output", f"{current_user.username}_output", filename)
    if not os.path.exists(output_path):
        return jsonify({"error": "File not found."}), 404
    return send_file(output_path, as_attachment=True)


@app.route("/download-pdf", methods=["GET"])
@login_required
def download_pdf():
    pdf_path = os.path.join("output", f"{current_user.username}_output", "notes.pdf")
    if not os.path.exists(pdf_path):
        return jsonify({"error": "PDF not found."}), 404
    return send_file(pdf_path, mimetype="application/pdf", as_attachment=True, download_name="study_notes.pdf")


# ── Quiz ───────────────────────────────────────────────────────────────────────

@app.route("/generate-quiz", methods=["POST"])
@login_required
def generate_quiz_endpoint():
    data     = request.get_json() or {}
    script   = data.get("script")
    video_id = data.get("video_id")

    if not script:
        return jsonify({"error": "Script is required to generate quiz."}), 400

    quiz = generate_quiz(script)
    if not quiz:
        return jsonify({"error": "Quiz generation failed."}), 500

    # Persist if video_id provided and not already saved
    if video_id:
        video_rec = db.session.get(Video, int(video_id))
        if video_rec and video_rec.user_id == current_user.id:
            existing = Quiz.query.filter_by(video_id=video_id).first()
            if not existing:
                db.session.add(Quiz(video_id=video_id, questions=json.dumps(quiz)))
                db.session.commit()

    return jsonify({"success": True, "quiz": quiz}), 200


@app.route("/quiz/<int:video_id>", methods=["GET"])
@login_required
def get_quiz(video_id):
    video_rec = db.session.get(Video, video_id)
    if not video_rec or video_rec.user_id != current_user.id:
        return jsonify({"error": "Not found."}), 404
    quiz_rec = Quiz.query.filter_by(video_id=video_id).first()
    if not quiz_rec:
        return jsonify({"error": "Quiz not generated yet."}), 404
    return jsonify({"quiz": json.loads(quiz_rec.questions)}), 200


# ── History & progress ─────────────────────────────────────────────────────────

@app.route("/history", methods=["GET"])
@login_required
def history():
    page  = request.args.get("page", 1, type=int)
    limit = min(request.args.get("limit", 20, type=int), 100)
    q = Video.query.filter_by(user_id=current_user.id).order_by(Video.created_at.desc())
    pagination = q.paginate(page=page, per_page=limit, error_out=False)
    videos = [
        {
            "id":         v.id,
            "filename":   v.filename,
            "prompt_text": v.prompt_text,
            "status":     v.status,
            "task_id":    v.task_id,
            "created_at": v.created_at.isoformat(),
        }
        for v in pagination.items
    ]
    return jsonify({"videos": videos, "total": pagination.total, "page": page, "pages": pagination.pages}), 200


@app.route("/progress", methods=["GET"])
@login_required
def progress():
    info = get_progress(user_id=str(current_user.id))
    return jsonify(info), 200


# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

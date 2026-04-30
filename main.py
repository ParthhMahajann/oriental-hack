import os
import json
import asyncio
import logging
import subprocess
from pathlib import Path
from jinja2 import Template
import tempfile
import shutil
from dotenv import load_dotenv

load_dotenv()  # must run before any module that reads env vars at import time

from google import genai
from google.genai import types
import edge_tts
from werkzeug.utils import secure_filename

from prompts import script_system_prompt, animation_system_prompt, pdf_system_prompt
from video import merge_with_ffmpeg, merge_videos
from animation import generate_html, record_animation
from helper import safe_launch, clear_folder, run_async_safely
from progress import set_progress
from pdf import extract_last_frame, generate_pdf

logger = logging.getLogger(__name__)

_genai_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

if os.name == "nt":
    possible_paths = [
        os.getenv("CHROME_PATH"),
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    CHROME_PATH = next((p for p in possible_paths if p and os.path.exists(p)), None) or "chrome"
else:
    CHROME_PATH = (
        os.getenv("CHROME_PATH")
        or shutil.which("google-chrome-stable")
        or shutil.which("google-chrome")
        or shutil.which("chromium-browser")
        or shutil.which("chromium")
        or shutil.which("chrome")
        or "/usr/bin/google-chrome-stable"
    )

logger.info("Using Chrome path: %s", CHROME_PATH)


# ── LLM ──────────────────────────────────────────────────────────────────────

def generate_response(msg_history, model="gemini-2.0-flash"):
    """Convert OpenAI-style message history and call Gemini."""
    messages = msg_history[:]
    system_instruction = None

    if messages and messages[0]["role"] == "system":
        system_instruction = messages[0]["content"]
        messages = messages[1:]

    def _to_gemini_role(role):
        return "user" if role == "user" else "model"

    contents = [
        types.Content(role=_to_gemini_role(m["role"]), parts=[types.Part(text=m["content"])])
        for m in messages
    ]

    config = types.GenerateContentConfig(system_instruction=system_instruction) if system_instruction else None
    response = _genai_client.models.generate_content(model=model, contents=contents, config=config)
    return response.text


# ── TTS ───────────────────────────────────────────────────────────────────────

async def _tts_async(save_file_path, script):
    communicate = edge_tts.Communicate(script, voice="en-US-AriaNeural")
    await communicate.save(save_file_path)


def generate_voice(save_file_path, script):
    run_async_safely(_tts_async(save_file_path, script))


# ── Utilities ─────────────────────────────────────────────────────────────────

def safe_text(text):
    return text.encode("utf-8", errors="replace").decode("utf-8")


def extract_code_from_response(content):
    if isinstance(content, str):
        return content
    for block in content:
        if hasattr(block, 'type') and block.type == 'text':
            return block.text
    return None


def safe_parse_json(gpt_output):
    try:
        if gpt_output.startswith("```json"):
            gpt_output = gpt_output.strip()[7:-3].strip()
        elif gpt_output.startswith("```"):
            gpt_output = gpt_output.strip()[3:-3].strip()
        return json.loads(gpt_output)
    except json.JSONDecodeError as e:
        logger.error("JSON parsing failed: %s", e)
        return None


# ── Animation ─────────────────────────────────────────────────────────────────

def generate_valid_animation_code(prompt, max_attempts=3, task_id="global"):
    past_error = ""
    msg_history = [
        {"role": "system", "content": animation_system_prompt},
        {"role": "user", "content": prompt},
    ]
    for attempt in range(1, max_attempts + 1):
        logger.info("Generating animation code (attempt %d)", attempt)
        set_progress({"state": "processing", "step": f"Generating animation (attempt {attempt})", "message": prompt}, user_id=task_id)
        clean_code = generate_response(msg_history)
        try:
            is_valid, logs = run_async_safely(validate_code_in_browser(clean_code))
            past_error = "\n".join(logs) if isinstance(logs, list) else str(logs)
        except Exception as e:
            logger.warning("Validation error: %s", e)
            is_valid = False

        if is_valid:
            logger.info("Valid animation code generated on attempt %d", attempt)
            return clean_code
        else:
            msg_history.append({"role": "system", "content": clean_code})
            msg_history.append({"role": "user", "content": f"The code has an error: {past_error}. Fix it and regenerate the animation: {prompt}"})
            logger.warning("Animation code invalid, retrying...")

    raise RuntimeError("All attempts to generate valid animation code failed.")


async def validate_code_in_browser(js_code):
    html_template = """
    <html>
      <head>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.9.0/p5.min.js"></script>
        <script>
          window.onerror = function(msg, src, line, col, err) {
            console.error("JSERROR:" + msg);
          };
        </script>
      </head>
      <body>
        <script>
          try {
              {{ code }}
              window.__animationLoaded = true;
          } catch(e) {
              console.error("JSERROR: " + e.message);
          }
        </script>
      </body>
    </html>
    """
    rendered = Template(html_template).render(code=js_code)
    html_path = Path(tempfile.gettempdir()) / "validate_animation.html"
    html_path.write_text(rendered, encoding="utf-8")

    browser = await safe_launch(headless=True, args=["--no-sandbox"], executablePath=CHROME_PATH)
    page = await browser.newPage()
    logs = []
    page.on("console", lambda msg: logs.append(msg.text))
    try:
        await page.goto(f"file://{html_path}")
        await asyncio.sleep(3)
        success = await page.evaluate("window.__animationLoaded === true")
    except Exception:
        success = False
    finally:
        await browser.close()
    has_js_error = any("JSERROR:" in log for log in logs)
    return (success and not has_js_error, logs)


# ── Placeholder video ─────────────────────────────────────────────────────────

def generate_placeholder_video(segment_id, duration, seg_folder):
    placeholder_path = f"{seg_folder}/{segment_id}.webm"
    os.makedirs(seg_folder, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"color=c=black:s=1280x720:d={duration}",
                "-c:v", "libvpx", "-crf", "10", "-b:v", "1M",
                placeholder_path,
            ],
            check=True,
            capture_output=True,
        )
        logger.info("Placeholder video created for segment %s", segment_id)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to create placeholder video: %s", e.stderr.decode(errors="replace"))


# ── Main pipeline ─────────────────────────────────────────────────────────────

def generate_video(user_prompt, output_filename, username, task_id="global"):
    """Generate a full educational video. Returns (success: bool, script: list)."""

    seg_folder   = f"segments/{username}"
    voice_folder = f"voice/{username}"
    final_folder = f"final_videos/{username}"
    pdf_folder   = f"pdf_images/{username}"

    set_progress({"state": "processing", "step": "Initializing", "message": "Clearing folders"}, user_id=task_id)
    clear_folder(seg_folder)
    clear_folder(voice_folder)
    clear_folder(final_folder)
    clear_folder(pdf_folder)

    msg_history_script = [
        {"role": "system", "content": script_system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    set_progress({"state": "processing", "step": "Generating script", "message": user_prompt}, user_id=task_id)
    script = generate_response(msg_history_script)
    script = safe_parse_json(script)
    if not script:
        raise RuntimeError("Script generation returned invalid JSON")

    notes_list = []

    for segment in script:
        segment_id = segment["id"]
        voiceover  = segment["voice_script"]
        animation  = segment["animation"]
        duration   = segment["duration"]

        set_progress({"state": "processing", "step": f"Processing {segment_id}", "message": "Generating animation"}, user_id=task_id)
        animation_prompt = f"{animation} to last at least {duration} seconds. The voiceover for this is {voiceover}"

        try:
            animation_code = generate_valid_animation_code(animation_prompt, task_id=task_id)
        except RuntimeError as e:
            logger.warning("Animation code failed for %s: %s — using placeholder", segment_id, e)
            animation_code = None

        if animation_code is not None:
            html_path = generate_html(animation_code)
            set_progress({"state": "processing", "step": f"Recording {segment_id}", "message": "Capturing animation"}, user_id=task_id)
            try:
                run_async_safely(record_animation(html_path, segment_id, duration, segments_folder=seg_folder))
            except Exception as e:
                logger.warning("Recording failed for %s: %s — using placeholder", segment_id, e)
                generate_placeholder_video(segment_id, duration, seg_folder)
        else:
            generate_placeholder_video(segment_id, duration, seg_folder)

        set_progress({"state": "processing", "step": f"Voiceover for {segment_id}", "message": "Synthesising voice"}, user_id=task_id)
        generate_voice(f"{voice_folder}/{segment_id}.mp3", voiceover)

        set_progress({"state": "processing", "step": f"Merging {segment_id}", "message": "Combining audio and video"}, user_id=task_id)
        merge_with_ffmpeg(
            f"{seg_folder}/{segment_id}.webm",
            f"{voice_folder}/{segment_id}.mp3",
            f"{final_folder}/{segment_id}.mp4",
        )
        logger.info("Segment %s complete", segment_id)

        msg_history_pdf = [
            {"role": "system", "content": pdf_system_prompt},
            {"role": "user", "content": voiceover},
        ]
        pdf_content = safe_text(generate_response(msg_history_pdf))
        segment_img = extract_last_frame(
            f"{seg_folder}/{segment_id}.webm",
            f"{pdf_folder}/{segment_id}.png",
        )
        notes_list.append({"id": segment_id, "notes": pdf_content, "image_path": segment_img})

    set_progress({"state": "processing", "step": "Merging final video", "message": "Combining all segments"}, user_id=task_id)

    user_output_folder = os.path.join("output", f"{username}_output")
    os.makedirs(user_output_folder, exist_ok=True)
    final_output_path = os.path.join(user_output_folder, output_filename)
    merge_videos(final_folder, final_output_path)

    pdf_filename = os.path.join(user_output_folder, "notes.pdf")
    if os.path.exists(pdf_filename):
        os.remove(pdf_filename)
    generate_pdf(notes_list, pdf_filename)
    logger.info("PDF notes generated")

    set_progress({"state": "processing", "step": "Completed", "message": "Video ready"}, user_id=task_id)
    return True, script

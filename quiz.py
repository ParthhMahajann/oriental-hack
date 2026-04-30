import os
import re
import json
import logging
from google import genai
from prompts import quiz_system_prompt

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))


def generate_quiz(script):
    try:
        full_text = "\n".join([s['voice_script'] for s in script])
        response = _client.models.generate_content(
            model="gemini-2.0-flash",
            contents=quiz_system_prompt + "\n\nScript:\n" + full_text,
        )
        text = response.text

        questions = re.findall(
            r"Q:\s*(.*?)\nA\.\s*(.*?)\nB\.\s*(.*?)\nC\.\s*(.*?)\nD\.\s*(.*?)\nAnswer:\s*([A-D])",
            text,
            re.DOTALL,
        )
        return questions[:10] if len(questions) >= 10 else []
    except Exception:
        logger.exception("Quiz generation failed")
        return []


if __name__ == "__main__":
    with open("scripts.json", "r", encoding="utf-8") as f:
        script = json.load(f)
    questions = generate_quiz(script)
    print(questions)

🎓 Gyan AI
Gyan AI is an AI-powered educational content generator that creates animated videos with voiceovers and interactive quizzes — all from a single text prompt. It's designed for students, educators, and learners who want visually rich explanations without video editing, scripting, or animation experience.

✨ Features
🎬 Automatic Video Generation
Input a topic → get a full p5.js animation with voiceover

🧠 AI-Generated Quiz
After watching the video, learners can test their understanding with an interactive quiz scored out of 10

🗣️ Natural Voice Narration
Voiceovers are generated using OpenAI’s Text-to-Speech API

🧪 Animation Validation
JS animation code is validated in a headless browser before recording

📼 Video + Audio Merging
Captured animations are combined with narration to produce final .mp4 videos

🧰 Streamlit Web UI
A clean, dark-themed interface for learners and judges

🏗️ Tech Stack
Component	Tech Used
Frontend UI	Streamlit
Video Animation	p5.js (rendered via Pyppeteer + CCapture.js)
AI LLMs	OpenAI GPT-4o, Claude 3, Gemini 1.5
TTS Voice	OpenAI Text-to-Speech API
Quiz Gen	Gemini 1.5 Pro
Video Merge	FFmpeg
Backend Logic	Python (with asyncio + Jinja2)

<body>

  <h2>⚡ Quick Start</h2>

  <h3>📦 Backend Setup (Flask)</h3>
  <pre><code>pip install -r requirements.txt
flask db init
flask db migrate -m "Initial migration"
flask db upgrade
python app.py</code></pre>

  <h3>💻 Frontend Setup (React)</h3>
  <pre><code>cd frontend/vidlearn-frontend-main
npm install
npm run dev</code></pre>

  <h3>🚀 Production Build</h3>
  <pre><code>cd frontend/vidlearn-frontend-main
npm run build</code></pre>

  <p>Flask will automatically serve the frontend from <code>frontend/build</code>.</p>

</body>


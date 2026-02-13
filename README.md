# 🎵 Music Finder v2.1

# DEMO LINK: https://yt-music-parser1.onrender.com

Music Finder is a lightweight web application built with **FastAPI** that allows users to search for YouTube videos by song name and extract audio in **MP3 format**.

This project was created as a **learning project** to practice backend development, async programming, and working with external tools like `yt-dlp` and FFmpeg.

---

## ✨ Features

- 🔍 **YouTube Search**: Search videos by song title or keywords
- ⚡ **Fast Performance**: Async requests + caching for better speed
- 🎧 **MP3 Audio Extraction**: Converts video audio stream into MP3 (192kbps)
- 🧹 **Auto Cleanup**: Automatically removes old downloaded files
- 💻 **Modern UI**: Responsive design with smooth animations (Glassmorphism style)

---

## 🛠️ Tech Stack

- **Backend**: Python 3.9+, FastAPI, Uvicorn
- **Audio Tools**: `yt-dlp`, FFmpeg
- **Frontend**: HTML5, CSS3, JavaScript (Async/Await)

---

## 🚀 Quick Start

### Prerequisites

- Python 3.9 or higher
- FFmpeg installed and доступний у PATH

---

### Installation

1. Clone the repository:

```bash
git clone https://github.com/your-username/music-finder.git
cd music-finder
Install dependencies:

pip install -r requirements.txt
Run the project:

uvicorn main:app --reload --port 8001
Open in browser:

http://127.0.0.1:8001
⚙️ Configuration
You can configure the project using environment variables or a .env file:

PORT — Server port (default: 8001)

DOWNLOADS_DIR — Folder for MP3 files (default: ./downloads)

MAX_FILE_SIZE — Maximum allowed file size in bytes (default: 150MB)

📌 Project Goal
This project was built to practice:

FastAPI routing and async endpoints

working with background tasks

caching logic for performance optimization

integration with external CLI tools (yt-dlp, FFmpeg)

building a clean and responsive frontend

⚠️ Disclaimer
This project is for educational purposes only.
Users are responsible for соблюдання copyright laws and YouTube Terms of Service.
The author is not responsible for any misuse.

<<<<<<< HEAD
This project is licensed under the MIT License.

I successfully did this project with the help of YouTube.
=======
>>>>>>> a5fa372 (adding readme)

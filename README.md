# 🎵 Music Finder v2.1

A modern, high-performance web application designed to search for music on YouTube and download it in high-quality MP3 format. Built with FastAPI and `yt-dlp`.

## ✨ Features

- **Blazing Fast Search**: Real-time YouTube search with intelligent caching.
- **Premium UI**: Modern glassmorphic design with smooth animations and responsive layout.
- **MP3 Download**: High-quality audio extraction (192kbps).
- **Auto-Cleanup**: Automatically removes old downloads to save disk space.
- **Async Architecture**: Non-blocking search and download operations.

## 🛠️ Tech Stack

- **Backend**: Python 3.9+, FastAPI, uvicorn
- **Audio Processing**: `yt-dlp`, FFmpeg
- **Frontend**: Vanilla HTML5, CSS3 (Glassmorphism), Javascript (Async/Await)

## 🚀 Quick Start

### Prerequisites

- Python 3.9 or higher
- **FFmpeg** installed on your system (required for audio conversion)

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/music-finder.git
   cd music-finder
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the application:
   ```bash
   python main.py
   ```

4. Open your browser and navigate to:
   `http://127.0.0.1:8001`

## ⚙️ Configuration

You can configure the application using environment variables or a `.env` file:

- `PORT`: Server port (default: 8001)
- `DOWNLOADS_DIR`: Directory for MP3 files (default: `./downloads`)
- `MAX_FILE_SIZE`: Maximum allowed download size in bytes (default: 150MB)

## 📝 License

This project is licensed under the MIT License.

I successfully did this project with the help of YouTube.

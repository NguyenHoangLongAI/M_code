"""
keys.py — Gemini API Key Pool
==============================
Thêm keys của bạn vào list GEMINI_KEYS bên dưới.
Lấy key tại: https://aistudio.google.com/apikey

Mỗi tài khoản Google AI Studio Free có:
  - 15 RPM  (requests per minute)
  - 1500 RPD (requests per day)
  - 1M TPM  (tokens per minute)

Dùng nhiều key → tăng throughput khi một key bị 429.
"""

GEMINI_KEYS: list[str] = [
    # Thêm keys của bạn vào đây, mỗi key một dòng:
    # "AIzaSy...",
    # "AIzaSy...",
    # "AIzaSy...",
]

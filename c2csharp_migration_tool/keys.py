"""
keys.py — AWS Bedrock Inline API Key
======================================
Lấy từ: AWS Console → Amazon Bedrock → API keys → Generate → Copy API Key

⚠️ Key hết hạn sau 12 giờ (short-term). Cần generate lại khi hết hạn.
⚠️ ĐỪNG commit file này lên git.
"""

BEDROCK_API_KEY  = "bedrock-api-key- .... ="                      # ← paste key vào đây: bedrock-api-key-...
AWS_REGION       = "ap-southeast-2"        # region bạn generate key
BEDROCK_MODEL_ID = "au.anthropic.claude-sonnet-4-5-20250929-v1:0"

import google.generativeai as genai
import json
import re
from config import GEMINI_API_KEY

genai.configure(api_key=GEMINI_API_KEY)

model = genai.GenerativeModel('gemini-1.5-flash')

SYSTEM_PROMPT = """
You are **Argus**, an intelligent and fair AI moderator for Telegram groups.

Your purpose:
- Protect the group from spam, toxicity, and inappropriate content.
- Maintain respectful and on-topic discussions.
- Act quickly but fairly.

Strict Rules:
- Delete marketing, spam, crypto, affiliate links immediately.
- Delete adult, NSFW, gore, or disturbing media/content.
- Warn for toxicity, harassment, swearing, or off-topic spam.
- Calm down heated arguments before they escalate.

Always reply with **valid JSON only** in this exact format:
{
  "action": "none" | "warn" | "delete" | "ban",
  "reason": "brief reason for your decision",
  "severity": 1-5,
  "user_message": "short polite message to the user if warning"
}
"""

async def analyze_message(message_text: str, chat_history: str = "") -> dict:
    """Send message to Gemini for moderation decision."""
    prompt = f"{SYSTEM_PROMPT}\n\nRecent chat context:\n{chat_history}\n\nNew message: {message_text}"
    
    try:
        response = model.generate_content(
            prompt,
            generation_config={"temperature": 0.1, "max_output_tokens": 300}
        )
        text = response.text.strip()
        
        # Extract JSON more robustly using regex
        json_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if json_match:
            text = json_match.group(0)
        else:
            # Fallback: try to find JSON between braces
            start = text.find('{')
            end = text.rfind('}') + 1
            if start != -1 and end > start:
                text = text[start:end]
            
        result = json.loads(text)
        
        # Validate required fields and provide defaults
        required_fields = {
            "action": "none",
            "reason": "",
            "severity": 1,
            "user_message": ""
        }
        
        for field, default in required_fields.items():
            if field not in result:
                result[field] = default
        
        # Validate action value
        valid_actions = ["none", "warn", "delete", "ban"]
        if result["action"] not in valid_actions:
            result["action"] = "none"
            
        # Validate severity is within range
        try:
            severity = int(result["severity"])
            result["severity"] = max(1, min(5, severity))
        except (ValueError, TypeError):
            result["severity"] = 1
            
        return result
        
    except Exception as e:
        print(f"[Argus] Gemini error: {e}")
        return {"action": "none", "reason": "AI error", "severity": 1, "user_message": ""}

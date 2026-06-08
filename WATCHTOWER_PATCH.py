"""
WATCHTOWER PATCH — add this to watchtower.py on the VPS

1. Add these two config variables near the top of watchtower.py
   (after the existing config block):

RAILWAY_API_URL = os.environ.get('RAILWAY_API_URL', '')
WATCHTOWER_SECRET = os.environ.get('WATCHTOWER_SECRET', '')

2. Add this function anywhere before the main() function:

def post_to_railway(status_data):
    \"\"\"POST status JSON to Railway-hosted API for HTTPS serving.\"\"\"
    if not RAILWAY_API_URL:
        logger.warning("RAILWAY_API_URL not configured, skipping Railway post")
        return
    try:
        headers = {'Content-Type': 'application/json'}
        if WATCHTOWER_SECRET:
            headers['X-Watchtower-Secret'] = WATCHTOWER_SECRET
        response = requests.post(
            f"{RAILWAY_API_URL}/status",
            json=status_data,
            headers=headers,
            timeout=10
        )
        if response.status_code == 200:
            logger.info(f"Posted status to Railway: {status_data.get('overall_status')}")
        else:
            logger.warning(f"Railway post returned {response.status_code}")
    except Exception as e:
        logger.warning(f"Failed to post to Railway: {e}")

3. In the main() function, after the line that writes status.json
   (after: logger.info(f"Status written to ..."))
   add this call:

    post_to_railway(status)

That's it. Watchtower will now POST to Railway after every run.
"""

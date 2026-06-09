import os
import json
import datetime
import requests
import tempfile
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

# 1. Pakistan Time Zone (UTC + 5) Setup
now_utc = datetime.datetime.utcnow()
pkt_hour = (now_utc.hour + 5) % 24
print(f"Current UTC Time: {now_utc.strftime('%H:%M')}, Current PKT Hour: {pkt_hour}")

# 2. Pages Aur Google Drive Sub-folders Ki Configuration
PAGES_CONFIG = {
    "DESI_DHAMAL_PAGE": {
        "page_id": "2160579077602705",
        "token_env": "DESI_DHAMAL_TOKEN",
        "folder_id": "1dZmdO7TbmA1sUFEJH4x1Elpe7DAZaGcV",
        "active_hours": [15, 18, 20, 22]  # PKT hours
    },
    "THE_AI_EFFECT_PAGE": {
        "page_id": "346054295247848",
        "token_env": "THE_AI_EFFECT_TOKEN",
        "folder_id": "14ofoHtSIhCS0B4uvvwivHfcJeFXlKNH2",
        "active_hours": [14, 18, 21]  # PKT hours
    }
}

# 3. Google Drive Service Authorization
try:
    creds_json = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    SCOPES = ['https://www.googleapis.com/auth/drive']
    creds = service_account.Credentials.from_service_account_info(creds_json, scopes=SCOPES)
    drive_service = build('drive', 'v3', credentials=creds)
    print("Google Drive authorization successful.")
except Exception as e:
    print("Google Authentication Error:", e)
    exit(1)


def download_from_drive(file_id, file_name):
    """Google Drive se video locally download karo GitHub runner par."""
    print(f"  Downloading '{file_name}' from Google Drive...")
    
    # /tmp folder mein save karo
    local_path = os.path.join(tempfile.gettempdir(), file_name)
    
    request = drive_service.files().get_media(fileId=file_id)
    
    with io.FileIO(local_path, 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=10 * 1024 * 1024)  # 10MB chunks
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"  Download progress: {int(status.progress() * 100)}%")
    
    file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
    print(f"  Download complete! File size: {file_size_mb:.1f} MB -> {local_path}")
    return local_path


def upload_to_facebook(page_id, page_token, local_video_path, title, description):
    """Local video file ko directly Facebook Graph API par binary upload karo."""
    print(f"  Uploading video to Facebook (binary upload)...")
    
    fb_url = f"https://graph.facebook.com/v19.0/{page_id}/videos"
    
    payload = {
        'title': title,
        'description': description,
        'access_token': page_token
    }
    
    with open(local_video_path, 'rb') as video_file:
        files = {
            'source': (os.path.basename(local_video_path), video_file, 'video/mp4')
        }
        response = requests.post(fb_url, data=payload, files=files, timeout=300)
    
    return response.json()


def get_and_post_video(page_name, config):
    page_id = config["page_id"]
    page_token = os.environ.get(config["token_env"])
    folder_id = config["folder_id"]

    if not page_token:
        print(f"[{page_name}] ERROR: Facebook token nahi mila GitHub Secrets mein! ({config['token_env']})")
        return

    # Step 1: Drive folder se pehli video dhundo
    print(f"[{page_name}] Searching for video in Google Drive folder...")
    try:
        results = drive_service.files().list(
            q=f"'{folder_id}' in parents and mimeType contains 'video/' and trashed = false",
            fields="files(id, name, size)",
            orderBy="createdTime",
            pageSize=1
        ).execute()
    except Exception as e:
        print(f"[{page_name}] Google Drive error while fetching files:", e)
        return

    files = results.get('files', [])
    if not files:
        print(f"[{page_name}] Koi video nahi mili Drive folder mein. Skipping.")
        return

    video_file = files[0]
    file_id = video_file['id']
    file_name = video_file['name']
    title = os.path.splitext(file_name)[0]

    print(f"[{page_name}] Video found: '{file_name}'")

    # Step 2: Video locally download karo
    local_path = None
    try:
        local_path = download_from_drive(file_id, file_name)
    except Exception as e:
        print(f"[{page_name}] Download failed:", e)
        return

    # Step 3: Facebook par binary upload karo
    try:
        fb_result = upload_to_facebook(page_id, page_token, local_path, title, title)
    except Exception as e:
        print(f"[{page_name}] Facebook upload error:", e)
        # Local temp file clean karo
        if local_path and os.path.exists(local_path):
            os.remove(local_path)
        return
    finally:
        # Local temp file hamesha clean karo (success ya fail)
        if local_path and os.path.exists(local_path):
            os.remove(local_path)
            print(f"[{page_name}] Temp file cleaned up.")

    # Step 4: Result check karo
    if "id" in fb_result:
        print(f"[{page_name}] SUCCESS! Video posted. FB Video ID: {fb_result['id']}")

        # Step 5: Drive se video delete karo (taake agli baar naya video use ho)
        try:
            drive_service.files().delete(fileId=file_id).execute()
            print(f"[{page_name}] Video deleted from Google Drive.")
        except Exception as e:
            print(f"[{page_name}] WARNING: Video posted but Drive delete failed:", e)
    else:
        print(f"[{page_name}] Facebook API Error:", fb_result)


# ========================================
# Main Execution
# ========================================
print(f"\n--- Running scheduler for PKT Hour: {pkt_hour} ---")

any_page_run = False
for page, cfg in PAGES_CONFIG.items():
    if pkt_hour in cfg["active_hours"]:
        any_page_run = True
        print(f"\n[MATCH] PKT Hour {pkt_hour} matches schedule for '{page}'. Starting...")
        get_and_post_video(page, cfg)

if not any_page_run:
    print(f"No page scheduled for PKT Hour {pkt_hour}. No action taken.")

print("\n--- Script finished ---")

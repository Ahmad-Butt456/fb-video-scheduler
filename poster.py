import os
import json
import datetime
import requests
import tempfile
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
from concurrent.futures import ThreadPoolExecutor

# 1. Pakistan Time Zone (UTC + 5) Setup & Robust Time Rounding (30-min window)
now_utc = datetime.datetime.utcnow()
pkt_now = now_utc + datetime.timedelta(hours=5)

# Robust minute rounding to :00 or :30 to tolerate GitHub Actions launch delays
minute = pkt_now.minute
if minute < 15:
    rounded_minute = 0
elif minute < 45:
    rounded_minute = 30
else:
    rounded_minute = 0
    pkt_now = pkt_now + datetime.timedelta(hours=1)

pkt_hour = pkt_now.hour
pkt_time_str = f"{pkt_now.hour:02d}:{rounded_minute:02d}"
print(f"Current UTC Time: {now_utc.strftime('%H:%M')}, Rounded PKT Time: {pkt_time_str}")

# 2. Google Drive Service Authorization
try:
    creds_json = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    SCOPES = ['https://www.googleapis.com/auth/drive']
    creds = service_account.Credentials.from_service_account_info(creds_json, scopes=SCOPES)
    drive_service = build('drive', 'v3', credentials=creds)
    print("Google Drive authorization successful.")
except Exception as e:
    print("Google Authentication Error:", e)
    exit(1)


def load_configs_from_drive():
    """Google Drive se central config file search aur load karti hai."""
    print("Loading pages configurations from Google Drive...")
    try:
        q = "name = 'fb_pages_config.json' and mimeType = 'application/json' and trashed = false"
        results = drive_service.files().list(q=q, fields="files(id)").execute()
        files = results.get('files', [])
        
        if not files:
            raise Exception("ERROR: 'fb_pages_config.json' file Google Drive par nahi mili! Service Account ke sath share karein.")
        
        config_file_id = files[0]['id']
        request = drive_service.files().get_media(fileId=config_file_id)
        
        file_io = io.BytesIO()
        downloader = MediaIoBaseDownload(file_io, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
            
        file_io.seek(0)
        config_data = json.loads(file_io.read().decode('utf-8'))
        print(f"Successfully loaded configs for {len(config_data)} pages from Google Drive.")
        return config_data
    except Exception as e:
        print("Failed to load configs from Drive:", e)
        exit(1)


def get_or_create_posted_folder(parent_folder_id):
    """Page folder ke andar 'Posted' folder dhundta hai, ya naya banata hai."""
    try:
        q = f"'{parent_folder_id}' in parents and name = 'Posted' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        results = drive_service.files().list(q=q, fields="files(id)").execute()
        files = results.get('files', [])
        
        if files:
            return files[0]['id']
        
        # Agar nahi mila toh naya banayein
        folder_metadata = {
            'name': 'Posted',
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_folder_id]
        }
        folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
        return folder.get('id')
    except Exception as e:
        print(f"Error managing 'Posted' folder for parent {parent_folder_id}: {e}")
        return None


def download_from_drive(file_id, file_name):
    """Google Drive se video locally download karo GitHub runner par."""
    local_path = os.path.join(tempfile.gettempdir(), file_name)
    request = drive_service.files().get_media(fileId=file_id)
    
    with io.FileIO(local_path, 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=10 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
    return local_path


def upload_to_facebook(page_id, page_token, local_video_path, title, description):
    """Local video file ko directly Facebook Graph API par binary upload karo."""
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
        response = requests.post(fb_url, data=payload, files=files, timeout=400)
    return response.json()


def get_and_post_video(page_name, config):
    page_id = config["page_id"]
    page_token = config["page_token"]
    folder_id = config["folder_id"]

    if not page_token or not page_id or not folder_id:
        print(f"[{page_name}] ERROR: Missing configuration data (token, ID, or folder).")
        return

    # Step 1: Drive folder se sabse purani video dhundo (orderBy createdTime)
    try:
        results = drive_service.files().list(
            q=f"'{folder_id}' in parents and mimeType contains 'video/' and trashed = false",
            fields="files(id, name)",
            orderBy="createdTime",
            pageSize=1
        ).execute()
    except Exception as e:
        print(f"[{page_name}] Google Drive error while fetching files:", e)
        return

    files = results.get('files', [])
    if not files:
        print(f"[{page_name}] No videos found in Drive folder.")
        return

    video_file = files[0]
    file_id = video_file['id']
    file_name = video_file['name']
    title = os.path.splitext(file_name)[0]

    print(f"[{page_name}] Processing video: '{file_name}'...")

    # Step 2: Download
    local_path = None
    try:
        local_path = download_from_drive(file_id, file_name)
    except Exception as e:
        print(f"[{page_name}] Download failed:", e)
        return

    # Step 3: Facebook Upload
    try:
        fb_result = upload_to_facebook(page_id, page_token, local_path, title, title)
    except Exception as e:
        print(f"[{page_name}] Facebook upload error:", e)
        return
    finally:
        # Local cleanup
        if local_path and os.path.exists(local_path):
            os.remove(local_path)

    # Step 4: Result Verification & Move to Posted Folder
    if "id" in fb_result:
        print(f"[{page_name}] SUCCESS! FB Video ID: {fb_result['id']}")

        # Move file to 'Posted' folder
        posted_folder_id = get_or_create_posted_folder(folder_id)
        if posted_folder_id:
            try:
                drive_service.files().update(
                    fileId=file_id,
                    addParents=posted_folder_id,
                    removeParents=folder_id,
                    fields='id, parents'
                ).execute()
                print(f"[{page_name}] Video successfully moved to 'Posted' folder.")
            except Exception as e:
                print(f"[{page_name}] WARNING: Video posted but failed to move in Drive:", e)
        else:
            print(f"[{page_name}] WARNING: Could not resolve 'Posted' folder. File left in place.")
    else:
        print(f"[{page_name}] Facebook API Error:", fb_result)


# ========================================
# Main Parallel Execution
# ========================================
# Load dynamically from Drive
PAGES_CONFIG = load_configs_from_drive()

print(f"\n--- Running scheduler for PKT Time: {pkt_time_str} ---")

active_pages = []
for page, cfg in PAGES_CONFIG.items():
    # Dono support karte hain: naya 'active_times' (e.g. ["12:30"]) aur purana 'active_hours' (e.g. [14, 18])
    active_times = cfg.get("active_times", [])
    active_hours = cfg.get("active_hours", [])
    
    is_active = False
    if pkt_time_str in active_times:
        is_active = True
    elif pkt_hour in active_hours:
        is_active = True
        
    if is_active:
        active_pages.append((page, cfg))

if active_pages:
    print(f"Found {len(active_pages)} pages active at this hour. Starting parallel upload pipeline...")
    
    # 20+ pages ke liye parallel processing (Max 5 threads at once to avoid CPU/Network overload)
    with ThreadPoolExecutor(max_workers=5) as executor:
        for page, cfg in active_pages:
            executor.submit(get_and_post_video, page, cfg)
else:
    print(f"No pages scheduled for PKT Hour {pkt_hour}.")

print("\n--- Script finished ---")

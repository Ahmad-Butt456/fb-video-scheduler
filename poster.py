import os
import json
import datetime
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# 1. Pakistan Time Zone (UTC + 5) Setup
now_utc = datetime.datetime.utcnow()
pkt_hour = (now_utc.hour + 5) % 24
print(f"Current UTC Time: {now_utc.strftime('%H:%M')}, Current PKT Hour: {pkt_hour}")

# 2. Pages Aur Google Drive Sub-folders Ki Configuration
# YAAD RAHI: Yahan apni asli Page IDs aur Google Drive Sub-folder IDs dalein!
PAGES_CONFIG = {
    "DESI_DHAMAL_PAGE": {
        "page_id": "2160579077602705", 
        "token_env": "DESI_DHAMAL_TOKEN",
        "folder_id": "1dZmdO7TbmA1sUFEJH4x1Elpe7DAZaGcV", 
        "active_hours": [15, 18, 20, 22] # 3:30 PM (Hour 15), 6 PM (18), 8 PM (20), 10 PM (22)
    },
    "THE_AI_EFFECT_PAGE": {
        "page_id": "346054295247848",
        "token_env": "THE_AI_EFFECT_TOKEN",
        "folder_id": "14ofoHtSIhCS0B4uvvwivHfcJeFXlKNH2", 
        "active_hours": [14, 18, 21] # 2 PM (14), 6 PM (18), 9 PM (21)
    }
}

# 3. Google Drive Service Authorization
try:
    creds_json = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    creds = service_account.Credentials.from_service_account_info(creds_json)
    drive_service = build('drive', 'v3', credentials=creds)
except Exception as e:
    print("Google Authentication Error:", e)
    exit(1)

def get_and_post_video(page_name, config):
    page_id = config["page_id"]
    page_token = os.environ.get(config["token_env"])
    folder_id = config["folder_id"]
    
    if not page_token:
        print(f"[{page_name}] Error: Facebook token nahi mila GitHub secrets me!")
        return

    # Folder se sab se pehli available video list karna
    try:
        results = drive_service.files().list(
            q=f"'{folder_id}' in parents and mimeType contains 'video/' and trashed = false",
            fields="files(id, name)",
            pageSize=1
        ).execute()
    except Exception as e:
        print(f"[{page_name}] Google Drive error while fetching files:", e)
        return
    
    files = results.get('files', [])
    if not files:
        print(f"[{page_name}] Koyi video nahi mili is folder me. Script checking completed.")
        return

    video_file = files[0]
    file_id = video_file['id']
    # Extension (.mp4) mita kar sirf naam nikalna description ke liye
    file_name = os.path.splitext(video_file['name'])[0] 
    
    print(f"[{page_name}] Video mili: '{video_file['name']}'. Facebook par upload shuru...")
    
    # Authenticated Direct Download URL generate karna
    if not creds.valid:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
    
    direct_video_url = f"https://googleapis.com{file_id}?alt=media"
    headers = {"Authorization": f"Bearer {creds.token}"}
    
    # Facebook API Request Payload
    fb_url = f"https://facebook.com{page_id}/videos"
    payload = {
        'file_url': direct_video_url,
        'title': file_name,
        'description': file_name, 
        'access_token': page_token
    }
    
    # Facebook Server ko request send karna
    response = requests.post(fb_url, data=payload, headers=headers)
    fb_result = response.json()
    
    if "id" in fb_result:
        print(f"[{page_name}] SUCCESS! Video post ho gayi. FB Video ID: {fb_result['id']}")
        
        # Post kamyabi se hone ke baad Google Drive se file delete (Trash) karna
        try:
            drive_service.files().delete(fileId=file_id).execute()
            print(f"[{page_name}] Video Google Drive se permanently delete kar di gayi hai.")
        except Exception as e:
            print(f"[{page_name}] Video post ho gayi thi, lekin Drive se delete nahi ho saki:", e)
    else:
        print(f"[{page_name}] Facebook API Error:", fb_result)

# Main Execution: Check karna ke is hour me kaunsa page scheduled hai
any_page_run = False
for page, cfg in PAGES_CONFIG.items():
    if pkt_hour in cfg["active_hours"]:
        any_page_run = True
        print(f"Time match ho gaya! Current PKT Hour: {pkt_hour}. Processing {page}...")
        get_and_post_video(page, cfg)

if not any_page_run:
    print(f"Is hour ({pkt_hour} PKT) me koyi page schedule nahi hai. No action taken.")

import os
import json
import datetime
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
from concurrent.futures import ThreadPoolExecutor

def lambda_handler(event, context):
    # Retrieve Credentials
    creds_json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json_str:
        print("ERROR: GOOGLE_CREDENTIALS_JSON environment variable is missing!")
        return {
            "statusCode": 500,
            "body": "Missing GOOGLE_CREDENTIALS_JSON environment variable"
        }
    
    # 1. Pakistan Time Zone (UTC + 5) Setup
    now_utc = datetime.datetime.utcnow()
    pkt_now = now_utc + datetime.timedelta(hours=5)
    pkt_time_str = pkt_now.strftime('%H:%M')
    pkt_hour = pkt_now.hour
    
    print(f"Current UTC Time: {now_utc.strftime('%H:%M')}, Actual PKT Time: {pkt_time_str}")

    def is_time_match(scheduled_time_str, tolerance_minutes=15):
        """
        Checks if the scheduled time (HH:MM) is within +/- tolerance_minutes of the current PKT time.
        Since AWS EventBridge runs on time, 15 minutes tolerance is perfect for 30-minute schedules.
        """
        try:
            sched_h, sched_m = map(int, scheduled_time_str.split(':'))
            scheduled = datetime.timedelta(hours=sched_h, minutes=sched_m)
            current = datetime.timedelta(hours=pkt_now.hour, minutes=pkt_now.minute)
            
            diff = abs((current - scheduled).total_seconds() / 60)
            # Handle midnight wrap-around (e.g. 23:50 vs 00:05)
            diff = min(diff, 24 * 60 - diff)
            
            return diff <= tolerance_minutes
        except Exception as e:
            print(f"Time matching error for {scheduled_time_str}: {e}")
            return False

    # 2. Google Drive Service Authorization
    try:
        creds_json = json.loads(creds_json_str)
        SCOPES = ['https://www.googleapis.com/auth/drive']
        creds = service_account.Credentials.from_service_account_info(creds_json, scopes=SCOPES)
        drive_service = build('drive', 'v3', credentials=creds)
        print("Google Drive authorization successful.")
    except Exception as e:
        print("Google Authentication Error:", e)
        return {
            "statusCode": 500,
            "body": f"Google Authentication Error: {str(e)}"
        }

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
            raise e

    def get_or_create_posted_folder(parent_folder_id):
        """Page folder ke andar 'Posted' folder dhundta hai, ya naya banata hai."""
        try:
            q = f"'{parent_folder_id}' in parents and name = 'Posted' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            results = drive_service.files().list(q=q, fields="files(id)").execute()
            files = AntiquatedFiles = results.get('files', [])
            
            if files:
                return files[0]['id']
            
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

    def make_file_public(file_id):
        """Google Drive file ko temporarily anyone-with-link public karo."""
        permission = {'type': 'anyone', 'role': 'reader'}
        result = drive_service.permissions().create(
            fileId=file_id, body=permission, fields='id'
        ).execute()
        return result.get('id')

    def revoke_public_access(file_id, permission_id):
        """Public permission hata do (file wapis private)."""
        try:
            drive_service.permissions().delete(
                fileId=file_id, permissionId=permission_id
            ).execute()
        except Exception as e:
            print(f"  Warning: Could not revoke public access: {e}")

    def upload_via_public_url(page_id, page_token, file_id, title, description):
        """Facebook ko Google Drive ka public URL pass karo."""
        public_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
        fb_url = f"https://graph.facebook.com/v19.0/{page_id}/videos"
        payload = {
            'file_url': public_url,
            'title': title,
            'description': description,
            'access_token': page_token
        }
        response = requests.post(fb_url, data=payload, timeout=120)
        return response.json()

    def get_and_post_video(page_name, config):
        page_id = config["page_id"]
        page_token = config["page_token"]
        folder_id = config["folder_id"]

        if not page_token or not page_id or not folder_id:
            print(f"[{page_name}] ERROR: Missing configuration data.")
            return

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

        permission_id = None
        fb_result = None
        try:
            print(f"[{page_name}] Making file temporarily public...")
            permission_id = make_file_public(file_id)
            print(f"[{page_name}] File is now public. Passing URL to Facebook...")
            fb_result = upload_via_public_url(page_id, page_token, file_id, title, title)
        except Exception as e:
            print(f"[{page_name}] Error during upload execution: {e}")
        finally:
            if permission_id:
                revoke_public_access(file_id, permission_id)
                print(f"[{page_name}] Public access revoked. File is private again.")

        if not fb_result:
            return

        if "id" in fb_result:
            print(f"[{page_name}] SUCCESS! FB Video ID: {fb_result['id']}")
            posted_folder_id = get_or_create_posted_folder(folder_id)
            if posted_folder_id:
                try:
                    drive_service.files().update(
                        fileId=file_id,
                        addParents=posted_folder_id,
                        removeParents=folder_id,
                        fields='id, parents'
                    ).execute()
                    print(f"[{page_name}] Video moved to 'Posted' folder.")
                except Exception as e:
                    print(f"[{page_name}] WARNING: Posted but failed to move in Drive:", e)
        else:
            print(f"[{page_name}] Facebook API Error:", fb_result)

    # Main logic execution
    try:
        PAGES_CONFIG = load_configs_from_drive()
    except Exception as e:
        return {
            "statusCode": 500,
            "body": f"Failed to load configurations: {str(e)}"
        }

    print(f"\n--- Running Lambda scheduler for PKT Time: {pkt_time_str} ---")

    active_pages = []
    for page, cfg in PAGES_CONFIG.items():
        active_times = cfg.get("active_times", [])
        active_hours = cfg.get("active_hours", [])

        is_active = False
        # Active times matching
        for t in active_times:
            if is_time_match(t):
                is_active = True
                print(f"[{page}] Time match: scheduled '{t}', current PKT '{pkt_time_str}'")
                break
        
        # Backwards compatibility with active hours
        if not is_active and pkt_hour in active_hours:
            is_active = True

        if is_active:
            active_pages.append((page, cfg))

    if active_pages:
        print(f"Found {len(active_pages)} active pages. Starting parallel uploads...")
        with ThreadPoolExecutor(max_workers=5) as executor:
            for page, cfg in active_pages:
                executor.submit(get_and_post_video, page, cfg)
    else:
        print(f"No pages scheduled for PKT Time {pkt_time_str}.")

    print("\n--- Execution finished ---")
    return {
        "statusCode": 200,
        "body": f"Executed successfully. Processed {len(active_pages)} pages."
    }
import os
import json
import datetime
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
from concurrent.futures import ThreadPoolExecutor

def lambda_handler(event, context):
    # Retrieve Credentials
    creds_json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json_str:
        print("ERROR: GOOGLE_CREDENTIALS_JSON environment variable is missing!")
        return {
            "statusCode": 500,
            "body": "Missing GOOGLE_CREDENTIALS_JSON environment variable"
        }
    
    # 1. Pakistan Time Zone (UTC + 5) Setup
    now_utc = datetime.datetime.utcnow()
    pkt_now = now_utc + datetime.timedelta(hours=5)
    pkt_time_str = pkt_now.strftime('%H:%M')
    pkt_hour = pkt_now.hour
    
    print(f"Current UTC Time: {now_utc.strftime('%H:%M')}, Actual PKT Time: {pkt_time_str}")

    def is_time_match(scheduled_time_str, tolerance_minutes=15):
        """
        Checks if the scheduled time (HH:MM) is within +/- tolerance_minutes of the current PKT time.
        Since AWS EventBridge runs on time, 15 minutes tolerance is perfect for 30-minute schedules.
        """
        try:
            sched_h, sched_m = map(int, scheduled_time_str.split(':'))
            scheduled = datetime.timedelta(hours=sched_h, minutes=sched_m)
            current = datetime.timedelta(hours=pkt_now.hour, minutes=pkt_now.minute)
            
            diff = abs((current - scheduled).total_seconds() / 60)
            # Handle midnight wrap-around (e.g. 23:50 vs 00:05)
            diff = min(diff, 24 * 60 - diff)
            
            return diff <= tolerance_minutes
        except Exception as e:
            print(f"Time matching error for {scheduled_time_str}: {e}")
            return False

    # 2. Google Drive Service Authorization
    try:
        creds_json = json.loads(creds_json_str)
        SCOPES = ['https://www.googleapis.com/auth/drive']
        creds = service_account.Credentials.from_service_account_info(creds_json, scopes=SCOPES)
        drive_service = build('drive', 'v3', credentials=creds)
        print("Google Drive authorization successful.")
    except Exception as e:
        print("Google Authentication Error:", e)
        return {
            "statusCode": 500,
            "body": f"Google Authentication Error: {str(e)}"
        }

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
            raise e

    def get_or_create_posted_folder(parent_folder_id):
        """Page folder ke andar 'Posted' folder dhundta hai, ya naya banata hai."""
        try:
            q = f"'{parent_folder_id}' in parents and name = 'Posted' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            results = drive_service.files().list(q=q, fields="files(id)").execute()
            files = AntiquatedFiles = results.get('files', [])
            
            if files:
                return files[0]['id']
            
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

    def make_file_public(file_id):
        """Google Drive file ko temporarily anyone-with-link public karo."""
        permission = {'type': 'anyone', 'role': 'reader'}
        result = drive_service.permissions().create(
            fileId=file_id, body=permission, fields='id'
        ).execute()
        return result.get('id')

    def revoke_public_access(file_id, permission_id):
        """Public permission hata do (file wapis private)."""
        try:
            drive_service.permissions().delete(
                fileId=file_id, permissionId=permission_id
            ).execute()
        except Exception as e:
            print(f"  Warning: Could not revoke public access: {e}")

    def upload_via_public_url(page_id, page_token, file_id, title, description):
        """Facebook ko Google Drive ka public URL pass karo."""
        public_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
        fb_url = f"https://graph.facebook.com/v19.0/{page_id}/videos"
        payload = {
            'file_url': public_url,
            'title': title,
            'description': description,
            'access_token': page_token
        }
        response = requests.post(fb_url, data=payload, timeout=120)
        return response.json()

    def get_and_post_video(page_name, config):
        page_id = config["page_id"]
        page_token = config["page_token"]
        folder_id = config["folder_id"]

        if not page_token or not page_id or not folder_id:
            print(f"[{page_name}] ERROR: Missing configuration data.")
            return

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

        permission_id = None
        fb_result = None
        try:
            print(f"[{page_name}] Making file temporarily public...")
            permission_id = make_file_public(file_id)
            print(f"[{page_name}] File is now public. Passing URL to Facebook...")
            fb_result = upload_via_public_url(page_id, page_token, file_id, title, title)
        except Exception as e:
            print(f"[{page_name}] Error during upload execution: {e}")
        finally:
            if permission_id:
                revoke_public_access(file_id, permission_id)
                print(f"[{page_name}] Public access revoked. File is private again.")

        if not fb_result:
            return

        if "id" in fb_result:
            print(f"[{page_name}] SUCCESS! FB Video ID: {fb_result['id']}")
            posted_folder_id = get_or_create_posted_folder(folder_id)
            if posted_folder_id:
                try:
                    drive_service.files().update(
                        fileId=file_id,
                        addParents=posted_folder_id,
                        removeParents=folder_id,
                        fields='id, parents'
                    ).execute()
                    print(f"[{page_name}] Video moved to 'Posted' folder.")
                except Exception as e:
                    print(f"[{page_name}] WARNING: Posted but failed to move in Drive:", e)
        else:
            print(f"[{page_name}] Facebook API Error:", fb_result)

    # Main logic execution
    try:
        PAGES_CONFIG = load_configs_from_drive()
    except Exception as e:
        return {
            "statusCode": 500,
            "body": f"Failed to load configurations: {str(e)}"
        }

    print(f"\n--- Running Lambda scheduler for PKT Time: {pkt_time_str} ---")

    active_pages = []
    for page, cfg in PAGES_CONFIG.items():
        active_times = cfg.get("active_times", [])
        active_hours = cfg.get("active_hours", [])

        is_active = False
        # Active times matching
        for t in active_times:
            if is_time_match(t):
                is_active = True
                print(f"[{page}] Time match: scheduled '{t}', current PKT '{pkt_time_str}'")
                break
        
        # Backwards compatibility with active hours
        if not is_active and pkt_hour in active_hours:
            is_active = True

        if is_active:
            active_pages.append((page, cfg))

    if active_pages:
        print(f"Found {len(active_pages)} active pages. Starting parallel uploads...")
        with ThreadPoolExecutor(max_workers=5) as executor:
            for page, cfg in active_pages:
                executor.submit(get_and_post_video, page, cfg)
    else:
        print(f"No pages scheduled for PKT Time {pkt_time_str}.")

    print("\n--- Execution finished ---")
    return {
        "statusCode": 200,
        "body": f"Executed successfully. Processed {len(active_pages)} pages."
    }

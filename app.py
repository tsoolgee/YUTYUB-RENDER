import os
import json
import requests
import threading
import uuid
import time
from flask import Flask, request, jsonify
import yt_dlp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)

# שליפת המשתנים מתוך הגדרות השרת ב-Render
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# מילון גלובלי בזיכרון השרת שישמור את מצב כל ההורדות שלנו
JOBS = {}

def get_drive_service():
    """התחברות מאובטחת ל-Google Drive API"""
    if not GOOGLE_CREDENTIALS_JSON:
        raise ValueError("שגיאה: משתנה הסביבה GOOGLE_CREDENTIALS_JSON חסר!")
    
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive.file"]
    )
    return build("drive", "v3", credentials=creds)

class FileStreamWrapper:
    """עטיפת זרם נתונים לקריאה במנות"""
    def __init__(self, raw_stream):
        self.raw_stream = raw_stream
        self.position = 0

    def read(self, size=-1):
        chunk = self.raw_stream.read(size)
        if chunk:
            self.position += len(chunk)
        return chunk

    def tell(self):
        return self.position

def get_direct_video_url(youtube_url):
    """חילוץ הקישור הישיר לקובץ הוידאו מיוטיוב"""
    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)
        return info['url'], info.get('title', 'video') + '.mp4'

def process_video_background(job_id, youtube_url):
    """
    הפונקציה הזו רצה ברקע! היא לא עוצרת את השרת מלהגיב לבקשות אחרות,
    ויש בה מנגנון התאוששות (Retry) במקרה של שגיאות רשת.
    """
    try:
        service = get_drive_service()
        
        JOBS[job_id]['status'] = 'מחפש נתונים...'
        direct_url, file_name = get_direct_video_url(youtube_url)
        JOBS[job_id]['file_name'] = file_name
        
        JOBS[job_id]['status'] = 'מתחבר לזרם...'
        response = requests.get(direct_url, stream=True)
        response.raise_for_status()
        
        stream_wrapper = FileStreamWrapper(response.raw)
        
        file_metadata = {
            'name': file_name,
            'parents': [GOOGLE_DRIVE_FOLDER_ID] if GOOGLE_DRIVE_FOLDER_ID else []
        }
        
        # גודל מנה - 5 מגה בייט. מאפשר חיסכון בזיכרון והתאוששות מהירה
        CHUNK_SIZE = 5 * 1024 * 1024 
        media = MediaIoBaseUpload(
            stream_wrapper, 
            mimetype='video/mp4', 
            chunksize=CHUNK_SIZE, 
            resumable=True
        )
        
        request_upload = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        )
        
        response_upload = None
        retries = 0
        MAX_RETRIES = 5 # ננסה עד 5 פעמים במקרה של ניתוק
        
        JOBS[job_id]['status'] = 'מעלה...'
        
        while response_upload is None:
            try:
                status, response_upload = request_upload.next_chunk()
                if status:
                    progress = int(status.progress() * 100)
                    JOBS[job_id]['progress'] = progress
                    retries = 0 # איפוס שגיאות במקרה של הצלחה
                    
            except Exception as chunk_error:
                retries += 1
                if retries > MAX_RETRIES:
                    raise Exception(f"ההעלאה נכשלה לאחר {MAX_RETRIES} ניסיונות. שגיאה אחרונה: {str(chunk_error)}")
                
                print(f"שגיאת רשת. מנסה מחדש בעוד כמה שניות (ניסיון {retries}/{MAX_RETRIES})")
                time.sleep(2 ** retries) # המתנה שהולכת וגדלה (2 שניות, 4 שניות, 8 שניות...) לפני הניסיון הבא

        JOBS[job_id]['status'] = 'הסתיים בהצלחה'
        JOBS[job_id]['drive_file_id'] = response_upload.get('id')
        JOBS[job_id]['progress'] = 100

    except Exception as e:
        JOBS[job_id]['status'] = 'שגיאה'
        JOBS[job_id]['error'] = str(e)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "השרת פועל וממתין"}), 200

@app.route('/download', methods=['POST'])
def start_download():
    """קבלת הבקשה והזנקת העבודה ברקע"""
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({"error": "חסר פרמטר 'url'"}), 400
    
    url = data['url']
    
    # יצירת מזהה עבודה ייחודי
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        'status': 'ממתין להתחלה',
        'progress': 0,
        'url': url
    }
    
    # פתיחת תהליך (Thread) שירוץ ברקע ולא יתקע את התשובה ללקוח
    thread = threading.Thread(target=process_video_background, args=(job_id, url))
    thread.daemon = True # מבטיח שהתהליך לא ימנע מהשרת להיסגר אם צריך
    thread.start()
    
    # השרת מחזיר תשובה מידית! מונע Timeout!
    return jsonify({
        "message": "התהליך התחיל לרוץ ברקע",
        "job_id": job_id,
        "status_url": f"/status/{job_id}"
    }), 202

@app.route('/status/<job_id>', methods=['GET'])
def get_status(job_id):
    """נתיב שמאפשר לבדוק מה המצב של כל עבודה, וגם משאיר את השרת ער"""
    job_info = JOBS.get(job_id)
    if not job_info:
        return jsonify({"error": "לא נמצאה עבודה כזו"}), 404
        
    return jsonify(job_info), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

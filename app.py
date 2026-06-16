import os
import json
import requests
from flask import Flask, request, jsonify
import yt_dlp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)

# שליפת המשתנים מתוך הגדרות השרת ב-Render
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

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
    """
    מחלקה שעוטפת את זרם הנתונים (Stream) כדי שה-API של גוגל
    יוכל לקרוא ממנו מנות בצורה תקנית באמצעות פונקציות read ו-tell.
    """
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
    """חילוץ הקישור הישיר לקובץ הוידאו מיוטיוב ללא שמירה לדיסק"""
    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # download=False זה הסוד כאן - רק מחלצים את המידע המטא-דאטה
        info = ydl.extract_info(youtube_url, download=False)
        return info['url'], info.get('title', 'video') + '.mp4'

def stream_to_drive(youtube_url):
    """פתיחת צינור הזרמה בין יוטיוב לגוגל דרייב"""
    service = get_drive_service()
    
    # 1. חילוץ הקישור הישיר מהרשת
    print("מחלץ קישור ישיר...")
    direct_url, file_name = get_direct_video_url(youtube_url)
    
    # 2. פתיחת חיבור רציף לזרם הנתונים מיוטיוב
    print(f"פותח חיבור לזרם הנתונים עבור: {file_name}")
    response = requests.get(direct_url, stream=True)
    response.raise_for_status()
    
    # 3. עטיפת הזרם במחלקה שלנו
    stream_wrapper = FileStreamWrapper(response.raw)
    
    file_metadata = {
        'name': file_name,
        'parents': [GOOGLE_DRIVE_FOLDER_ID] if GOOGLE_DRIVE_FOLDER_ID else []
    }
    
    # 4. הגדרת ההעלאה לדרייב במנות של 5MB
    CHUNK_SIZE = 5 * 1024 * 1024
    media = MediaIoBaseUpload(
        stream_wrapper, 
        mimetype='video/mp4', 
        chunksize=CHUNK_SIZE, 
        resumable=True
    )
    
    print("מתחיל העלאה ישירה לדרייב במנות...")
    request_upload = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id'
    )
    
    # 5. ריצת לולאה שמעלה מנה אחרי מנה עד לסיום הקובץ
    response_upload = None
    while response_upload is None:
        status, response_upload = request_upload.next_chunk()
        if status:
            print(f"הועלה {int(status.progress() * 100)}%")
            
    print(f"העלאה הסתיימה בהצלחה! מזהה קובץ: {response_upload.get('id')}")
    return response_upload.get('id')

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "השרת פועל וממתין להזרמות"}), 200

@app.route('/download', methods=['POST'])
def download_and_upload():
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({"error": "חסר פרמטר 'url' בבקשה"}), 400
    
    url = data['url']
    
    try:
        print(f"מתחיל תהליך עבור הקישור: {url}")
        
        # קריאה לפונקציית ההזרמה
        drive_file_id = stream_to_drive(url)
        
        return jsonify({
            "status": "success",
            "message": "הסרטון הוזרם בהצלחה לגוגל דרייב!",
            "drive_file_id": drive_file_id
        }), 200
        
    except Exception as e:
        print(f"שגיאה בתהליך: {str(e)}")
        return jsonify({"error": f"אירעה שגיאה: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

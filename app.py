"""
app.py - تشغيل الموقع محليا على الجهاز
-------------------------------------
شغّله:  python app.py   (أو دبل كليك على run.bat)
هيفتح المتصفح على http://127.0.0.1:8765
"""

import threading
import webbrowser

from api.index import BOOKS_DIR, OUT_DIR, app

PORT = 8765


def main():
    url = f"http://127.0.0.1:{PORT}"
    print(f"الموقع شغال على {url}  (اقفل الشباك ده عشان توقفه)")
    print(f"فولدر الكتب: {BOOKS_DIR}")
    print(f"فولدر النسخ: {OUT_DIR}")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

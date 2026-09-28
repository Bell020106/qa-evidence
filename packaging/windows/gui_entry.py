import sys
import traceback
from signup031.desktop_runtime import configure_runtime,user_base

try:
    configure_runtime()
    from signup031.viewer import main
    code=main()
except Exception:
    message=traceback.format_exc()
    destination=user_base()/'startup-error.log'
    try:
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(message,encoding='utf-8')
    except OSError:pass
    if '--render-png' not in sys.argv:
        from PySide6.QtWidgets import QApplication,QMessageBox
        application=QApplication.instance() or QApplication([])
        QMessageBox.critical(None,'QA Evidence 시작 실패','시작하지 못했습니다. 설치 파일로 복구하거나 오류 기록을 확인하세요.\n'+str(destination))
    code=2
raise SystemExit(code)

# -*- coding: utf-8 -*-
import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
import os
import time
import threading
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
import subprocess

# ----------------------------------------------------------------------
# データベースモード変更実行関数
# ----------------------------------------------------------------------
def change_db_journal_mode(db_path, target_mode="WAL"):
    """Windows/Linux対応：OSコマンドでjournal_modeを強制変更する"""
    import sys
    try:
        db_dir = os.path.dirname(db_path)
        db_name = os.path.basename(db_path)
        
        # target_modeがNoneなら確認、値があれば変更
        pragma_cmd = f"PRAGMA journal_mode={target_mode};" if target_mode else "PRAGMA journal_mode;"

        if sys.platform == "win32":
            # Windows: 手入力で成功した「cdで移動して実行」を再現
            cmd = f'cd /d "{db_dir}" && sqlite3 "{db_name}" "{pragma_cmd}"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        else:
            # Linux: パス指定で実行
            result = subprocess.run(['sqlite3', db_path, f'PRAGMA journal_mode={target_mode};'], 
                                   capture_output=True, text=True)
        
        if result.returncode == 0:
            return True, result.stdout.strip().split('\n')[-1].lower()
        return False, result.stderr.strip()
    except Exception as e:
        return False, str(e)

addon = xbmcaddon.Addon()
addon_name = addon.getAddonInfo('name')
dialog = xbmcgui.Dialog()

# ----------------------------------------------------------------------
# 通知用ロジック
# ----------------------------------------------------------------------
def send_notification_mail(subject, body):
    if not addon.getSettingBool("send_email"):
        return
    
    gmail_user = addon.getSetting("gmail_user")
    gmail_pass = addon.getSetting("gmail_pass")
    gmail_dest = addon.getSetting("gmail_dest")

    if not gmail_user or not gmail_pass:
        return

    try:
        import ssl
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = gmail_user
        msg['To'] = gmail_dest
        msg['Date'] = formatdate(localtime=True)

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        server = smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context)
        server.login(gmail_user, gmail_pass)
        server.send_message(msg)
        server.close()
    except Exception as e:
        xbmc.log(f"[{addon_name}] Email Error: {str(e)}", xbmc.LOGERROR)

# ----------------------------------------------------------------------
# XSPリペア（共通・検証 兼用ロジック）
# ----------------------------------------------------------------------
def scan_recursive(base_path, file_list):
    """【復元】元の再帰スキャン構造に戻し、最初に一度だけ正確に全ファイルを走査させる"""
    dirs, files = xbmcvfs.listdir(base_path)
    for file_name in files:
        if file_name.lower().endswith(".xsp"):
            full_path = os.path.join(base_path, file_name).replace('\\', '/')
            file_list.append(full_path)
    for dir_name in dirs:
        sub_dir_path = os.path.join(base_path, dir_name).replace('\\', '/') + "/"
        scan_recursive(sub_dir_path, file_list)

def run_xsp_repair(is_verification=False):
    import glob
    xbmc.log(f"[{addon_name}] Starting XSP Repair (Verify Mode: {is_verification})", xbmc.LOGINFO)
    
    wait_ms = addon.getSettingInt("delay_between_xsp")

    # Kodiの標準的なプレイリスト保存先（ミュージック/ビデオ）
    base_dirs = ["special://profile/playlists/music/", "special://profile/playlists/video/"]
    all_xsp_files = []

    # 【復元】ディスクスキャンはループの外側で1回のみ実行する元の安全な仕様に復元
    for base in base_dirs:
        if xbmcvfs.exists(base):
            scan_recursive(base, all_xsp_files)

    if not all_xsp_files:
        xbmc.log(f"[{addon_name}] No XSP files found. Proceeding to DB check.", xbmc.LOGINFO)

    # ループ回数（検証時のみ設定値を使用、通常は1回）
    loop_count = addon.getSettingInt("loop_count") if is_verification else 1

    # 【復元】ActivateWindow を使ったGUI強制描画シーケンスを完全復元
    for i in range(loop_count):
        for xsp_path in all_xsp_files:
            xbmc.executebuiltin(f'ActivateWindow(10502, "{xsp_path}", return)')
            timeout = time.time() + 5
            while not xbmc.getCondVisibility('Window.IsActive(music)') and time.time() < timeout:
                xbmc.sleep(200)
            # 画面固定のため設定画面へ移動
            xbmc.executebuiltin('ActivateWindow(settings)')
            xbmc.sleep(wait_ms)

    # データベース診断：MusicとVideoの両方を対象に実行
    db_results_text = ""
    has_non_wal = False
    db_abs_path = ""
    auto_convert = addon.getSettingBool("auto_wal_convert")
    
    try:
        db_path_kodi = xbmcvfs.translatePath('special://database/')
        db_abs_path = os.path.abspath(db_path_kodi)

        db_patterns = ['MyMusic*.db', 'MyVideo*.db']
        db_files_all = []
        for pattern in db_patterns:
            db_files_all.extend(glob.glob(os.path.join(db_abs_path, pattern)))

        db_files = sorted(list(set(db_files_all)))
        processed_names = [] 

        for db in db_files:
            db_name = os.path.basename(db)
            if db_name in processed_names:
                continue
            processed_names.append(db_name)
            
            success_diag, current_mode = change_db_journal_mode(db, target_mode=None)

            if not success_diag:
                db_results_text += f" ・ {db_name}: 判定スキップ（理由: {current_mode}）\n"
                continue

            final_status = current_mode.upper()
            if current_mode.lower() != 'wal':
                if auto_convert:
                    success, msg = change_db_journal_mode(db, "WAL")
                    if success:
                        final_status = f"CHANGED TO {msg.upper()}"
                    else:
                        final_status = f"STAY {current_mode.upper()} (ERR:{msg})"
                        has_non_wal = True
                else:
                    has_non_wal = True
            else:
                final_status = "WAL (OK)"
                
            db_results_text += f" ・ {db_name}: {final_status}\n"

    except Exception as e:
        db_results_text = f"判定スキップ（理由: {str(e)}）"

    # メール送信ロジック
    subj_set = addon.getSetting("mail_subject_verify" if is_verification else "mail_subject_normal")
    body_set = addon.getSetting("mail_body_template")
    diag_title = addon.getSetting("db_diag_title") or "■ データベース診断結果:"
    wal_warning = addon.getSetting("db_wal_warning") or "【重要】WALモードでないDBがあります。KODI終了後に実行を推奨："

    subject = subj_set if subj_set else ("【検証完了】" if is_verification else "メンテナンス完了報告")
    try:
        # 【復元】件数カウントを実際のユニークファイル数（len(all_xsp_files)）に完全復元
        main_text = body_set.format(count=len(all_xsp_files), mode='検証' if is_verification else '通常') if body_set else f"{'検証' if is_verification else '通常'}更新（{len(all_xsp_files)}件）を完了しました。"
    except:
        main_text = f"更新（{len(all_xsp_files)}件）を完了しました。"
    
    full_body = f"{main_text}\n\n{diag_title}\n{db_results_text if db_results_text else '対象DBなし'}"
    
    if has_non_wal and db_abs_path:
        full_body += f"\n\n{wal_warning}"
        full_body += f"\nsqlite3 \"{os.path.join(db_abs_path, 'MyMusic*.db')}\" \"PRAGMA journal_mode=WAL;\""
        full_body += f"\nsqlite3 \"{os.path.join(db_abs_path, 'MyVideo*.db')}\" \"PRAGMA journal_mode=WAL;\""

    # texturecache.py バックグラウンド自動実行ロジック
    if addon.getSettingBool("enable_texturecache"):
        import sys
        profile_dir = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
        script_path = os.path.join(profile_dir, "texturecache.py")
        
        if os.path.exists(script_path):
            try:
                # 外部プロセスの滞留を防ぐ非同期管理（これは正常に維持）
                cmd = ["nice", "-n", "19", "python3", script_path, "c", "audio"] if sys.platform != "win32" else ["python3", script_path, "c", "audio"]
                kwargs = {}
                if sys.platform == "win32":
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                
                # 独立した関数をターゲットに指定し、親関数の変数スコープがスレッドに紐付けられるのを遮断
                t = threading.Thread(target=_run_texturecache_async, args=(cmd, kwargs, addon_name))
                t.daemon = True
                t.start()
                full_body += "\n\n■ キャッシュ処理:\n ・ texturecache.py をバックグラウンドで安全に呼び出しました。"
            except Exception as tc_err:
                xbmc.log(f"[{addon_name}] Failed to start texturecache.py: {str(tc_err)}", xbmc.LOGERROR)
                full_body += f"\n\n■ キャッシュ処理:\n ・ 呼び出し失敗: {str(tc_err)}"
        else:
            full_body += "\n\n■ キャッシュ処理:\n ・ スクリプト未ダウンロードのためスキップされました。"

    send_notification_mail(subject, full_body)
    dialog.notification(addon_name, main_text, xbmcgui.NOTIFICATION_INFO, 5000)

    # 【修正】大容量リストオブジェクトを明示的に削除し、ガベージコレクションを強制。常駐サービス側でPythonメモリプールが抱え込み高止まりする現象を回避しOSへ領域を即時返却
    del all_xsp_files
    import gc
    gc.collect()

def _run_texturecache_async(cmd, kwargs, addon_name):
    """インナー関数によるメモリ（クロージャ）の抱え込みを防ぐため、完全に独立させた外部プロセス実行関数"""
    try:
        # 出力をDEVNULLに捨てることで大容量ログのメモリ蓄積を防止
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, **kwargs)
    except subprocess.TimeoutExpired:
        xbmc.log(f"[{addon_name}] texturecache.py execution timed out (300s limit exceeded). Resource forced cleanup.", xbmc.LOGWARNING)
    except:
        pass
        
# ----------------------------------------------------------------------
# 手動ダウンロード処理関数
# ----------------------------------------------------------------------
def manual_download_script():
    import sys
    url = addon.getSetting("texturecache_url")
    profile_dir = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    
    if not os.path.exists(profile_dir):
        os.makedirs(profile_dir)
        
    dest_path = os.path.join(profile_dir, "texturecache.py")
    
    if dialog.yesno("確認", f"設定されたURLから texturecache.py を取得します。\n保存先: {dest_path}\n\n実行してよろしいですか？"):
        try:
            import urllib.request
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            )
            with urllib.request.urlopen(req) as response:
                with open(dest_path, 'wb') as f:
                    f.write(response.read())
                    
            if sys.platform != "win32":
                os.chmod(dest_path, 0o755)
            dialog.ok("完了", "texturecache.py のダウンロードに成功しました。")
        except Exception as e:
            xbmc.log(f"[{addon_name}] Download Error: {str(e)}", xbmc.LOGERROR)
            dialog.ok("エラー", f"ダウンロードに失敗しました:\n{str(e)}")

# ----------------------------------------------------------------------
# 監視サービス本体
# ----------------------------------------------------------------------
class MyMonitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        # タイマーの代わりにフラグと次回実行時刻のみを管理
        self._pending_task = False
        self._execution_time = 0

    def onNotification(self, sender, method, data):
        targets = ("AudioLibrary.OnScanFinished", "AudioLibrary.OnCleanFinished",
                   "VideoLibrary.OnScanFinished", "VideoLibrary.OnCleanFinished")
        if method in targets:
            self.trigger_sequence(method)

    def trigger_sequence(self, source):
        # 新しいスレッド（Timer）を作らず、メインループ側で処理を遅延実行するためのスケジュールを設定
        delay = addon.getSettingInt("xsp_interval")
        self._execution_time = time.time() + (delay if delay > 0 else 5)
        self._pending_task = True

    def check_and_execute(self):
        # メインループから毎秒呼び出され、予定時刻が来たら実行する
        if self._pending_task and time.time() >= self._execution_time:
            self._pending_task = False
            self._execute_full_task()

    def run_sort_sequence(self):
        """ソート異常改善"""
        xbmc.executebuiltin('ActivateWindow(Music,musicdb://songs/)')
        xbmc.executebuiltin('Container.SetSortMethod(16)')
        xbmc.executebuiltin('Container.SetSortOrder(descending)')
        xbmc.sleep(1500)
        xbmc.executebuiltin('ActivateWindow(Music,musicdb://musics/)')
        xbmc.sleep(1000)
        xbmc.executebuiltin('ActivateWindow(Music,musicdb://songs/)')
        xbmc.executebuiltin('Container.SetSortMethod(8)')
        xbmc.executebuiltin('Container.SetSortOrder(ascending)')

    def _execute_full_task(self):
        xbmc.log(f"[{addon_name}] Starting maintenance tasks", xbmc.LOGINFO)
        
        # 1. XSP更新（通常モード）
        run_xsp_repair(is_verification=False)

        # 2. 共通設定の待機秒数を挟んでソート実行
        xbmc.sleep(addon.getSettingInt("delay_after_cleanup") * 1000)
        self.run_sort_sequence()

        # 3. 終了
        xbmc.sleep(1000)
        xbmc.executebuiltin('ActivateWindow(home)')

    def onSettingsChanged(self):
        if addon.getSettingBool("run_db_fix_toggle"):
            addon.setSettingBool("run_db_fix_toggle", False)
            xbmc.log(f"[{addon_name}] Manual DB Fix triggered from settings", xbmc.LOGINFO)
            run_xsp_repair(is_verification=False)

        if addon.getSettingBool("run_verify_toggle"):
            addon.setSettingBool("run_verify_toggle", False)
            xbmc.log(f"[{addon_name}] Manual Verification triggered from settings", xbmc.LOGINFO)
            run_xsp_repair(is_verification=True)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "download_script":
        manual_download_script()
    else:
        monitor = MyMonitor()
        xbmc.log(f"[{addon_name}] Service Started", xbmc.LOGINFO)
        while not monitor.waitForAbort(1):
            # 毎秒タイマーの代わりとなる実行チェックを行う
            monitor.check_and_execute()
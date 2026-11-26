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
# UPnPウォームアップ（Kodi再起動後の保存キュー再生不可対策）
# ----------------------------------------------------------------------
# 再起動で消えるKodiのMD5→実パス対応表は、UPnPのBrowseでアイテムが
# 構築されるたびに登録される。起動直後に全曲へBrowseMetadataを
# 1件ずつ送り、全曲を再登録する。
# 曲のObjectIDは音楽DBから生成する:
#   base64("musicdb://songs/<idSong>.<拡張子>")
# （2026-11-18 実証済み: DBの idSong=5210「yoake」と、
#   生成IDへのBrowseMetadata応答のタイトルが一致）
def _upnp_warmup_sleep(monitor, seconds):
    """待機。Kodi終了要求が来たらFalseを返す"""
    end = time.time() + seconds
    while time.time() < end:
        if monitor.waitForAbort(min(1.0, end - time.time())):
            return False
    return not monitor.abortRequested()

def _upnp_browse_metadata_once(control_url, object_id):
    """BrowseMetadataを1回送る。応答にResultが無ければ例外にする"""
    import urllib.request
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
            '<ObjectID>' + object_id + '</ObjectID>'
            '<BrowseFlag>BrowseMetadata</BrowseFlag>'
            '<Filter>*</Filter>'
            '<StartingIndex>0</StartingIndex>'
            '<RequestedCount>0</RequestedCount>'
            '<SortCriteria></SortCriteria>'
            '</u:Browse></s:Body></s:Envelope>').encode('utf-8')
    req = urllib.request.Request(control_url, data=body, headers={
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPAction': '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    resp = opener.open(req, timeout=30).read().decode('utf-8', 'replace')
    if '<Result>' not in resp:
        raise RuntimeError('no Result in UPnP response')

def _upnp_probe_system_update_id(control_url):
    """GetSystemUpdateIDで ContentDirectory の応答だけを確認する。
    特定の曲に依存しないので、接続確認（ゲート）専用に使う"""
    import urllib.request
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:GetSystemUpdateID xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
            '</u:GetSystemUpdateID></s:Body></s:Envelope>').encode('utf-8')
    req = urllib.request.Request(control_url, data=body, headers={
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPAction': '"urn:schemas-upnp-org:service:ContentDirectory:1#GetSystemUpdateID"'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    resp = opener.open(req, timeout=30).read().decode('utf-8', 'replace')
    if 'SystemUpdateID' not in resp:
        raise RuntimeError('no SystemUpdateID in UPnP response')

def _upnp_control_url_candidates():
    """制御URLの候補を返す。(候補リスト, 出どころ文字列)
    設定に手入力があればそれを最優先。無ければ、Kodi自身が
    userdata/upnpserver.xml に保存している自機のUUIDとポートから
    組み立てる（127.0.0.1 と 自機のLANアドレスの2候補）"""
    manual = addon.getSetting("warmup_control_url").strip()
    if manual:
        return [manual], "manual setting"
    try:
        import xml.etree.ElementTree as ET
        xml_path = os.path.join(xbmcvfs.translatePath('special://profile/'), 'upnpserver.xml')
        root = ET.parse(xml_path).getroot()
        uuid = (root.findtext('UUID') or '').strip()
        port = (root.findtext('Port') or '').strip()
        if not uuid or not port:
            xbmc.log(f"[{addon_name}] UPnP Warmup: upnpserver.xml has no UUID/Port yet ({xml_path})", xbmc.LOGERROR)
            return [], "auto (upnpserver.xml incomplete)"
        path = f"/ContentDirectory/{uuid}/control.xml"
        candidates = [f"http://127.0.0.1:{port}{path}"]
        # 自機のLANアドレス（UDP connectは実際には何も送信しない）
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(('8.8.8.8', 80))
                lan_ip = s.getsockname()[0]
            finally:
                s.close()
            if lan_ip and not lan_ip.startswith('127.'):
                candidates.append(f"http://{lan_ip}:{port}{path}")
        except Exception:
            pass
        return candidates, "auto (upnpserver.xml)"
    except Exception as e:
        xbmc.log(f"[{addon_name}] UPnP Warmup: failed to read upnpserver.xml: {str(e)}", xbmc.LOGERROR)
        return [], "auto (upnpserver.xml unreadable)"

def _upnp_server_uuid():
    """upnpserver.xml に保存された自機のUUIDだけを返す（無ければ空文字）。
    SSDP探索で自機のサーバーを識別するために使う"""
    try:
        import xml.etree.ElementTree as ET
        xml_path = os.path.join(xbmcvfs.translatePath('special://profile/'), 'upnpserver.xml')
        root = ET.parse(xml_path).getroot()
        return (root.findtext('UUID') or '').strip()
    except Exception:
        return ""

def _ssdp_discover_control_urls(uuid):
    """SSDP探索で自機Kodiの ContentDirectory 制御URLを探して返す。
    UPnPサーバー部品が、記録されたポートではなく動的ポートで待ち受けて
    いる場合（Kodiのみ再起動で前のプロセスがポートを離し切っていない等）
    でも、サーバーが告知している実際のURLが得られる。
    自機の識別はUUID（M-SEARCH応答のUSNと、機器説明のUDNで照合）"""
    import re
    import socket
    import urllib.parse
    import urllib.request
    if not uuid:
        return []
    locations = []
    try:
        msg = ('M-SEARCH * HTTP/1.1\r\n'
               'HOST: 239.255.255.250:1900\r\n'
               'MAN: "ssdp:discover"\r\n'
               'MX: 3\r\n'
               'ST: urn:schemas-upnp-org:service:ContentDirectory:1\r\n\r\n')
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.sendto(msg.encode('utf-8'), ('239.255.255.250', 1900))
            deadline = time.time() + 5
            while time.time() < deadline:
                sock.settimeout(max(0.1, deadline - time.time()))
                try:
                    data, _addr = sock.recvfrom(65535)
                except socket.timeout:
                    break
                headers = {}
                for line in data.decode('utf-8', 'replace').split('\r\n')[1:]:
                    if ':' in line:
                        k, v = line.split(':', 1)
                        headers[k.strip().lower()] = v.strip()
                loc = headers.get('location', '')
                if loc and uuid in headers.get('usn', '') and loc not in locations:
                    locations.append(loc)
        finally:
            sock.close()
    except Exception:
        return []
    results = []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for loc in locations:
        try:
            req = urllib.request.Request(loc)
            xml_text = opener.open(req, timeout=5).read().decode('utf-8', 'replace')
        except Exception:
            continue
        udn_m = re.search(r'<UDN>([^<]+)</UDN>', xml_text)
        if udn_m and uuid not in udn_m.group(1):
            continue  # 別機器の説明だった場合は使わない
        for block in re.findall(r'<service>(.*?)</service>', xml_text, re.S):
            if 'ContentDirectory' not in block:
                continue
            m = re.search(r'<controlURL>([^<]+)</controlURL>', block)
            if m:
                url = urllib.parse.urljoin(loc, m.group(1).strip())
                if url not in results:
                    results.append(url)
    return results

# セーフガードの既定間隔（ms）：正常終了の実績が無い・状態が不明なときの戻り先。
# 検証で詰めたくなったら、この1か所だけを直す
WARMUP_SAFE_INTERVAL_MS = 100

# 接続確認の最大待機（秒）：UPnPサーバーの待ち受け開始が遅い起動でも
# 諦めずに待ち続ける上限。これを超えたらこの回は打ち切る
WARMUP_PROBE_MAX_WAIT_SEC = 1800

# 読み込み順の設定値 → ORDER BY 句の固定対応表（設定文字列を直接 SQL に
# 入れないためのホワイトリスト。不明な値は ID順（古い順）として扱う）
WARMUP_ORDER_SQL = {
    'ID順（古い順）': 'idSong ASC',
    'ID順（新しい順）': 'idSong DESC',
    '追加日の新しい順': 'dateAdded DESC, idSong DESC',
    '曲名順': 'strTitle ASC, idSong ASC',
    'ランダム': 'RANDOM()',
}

def _warmup_state_path():
    return os.path.join(xbmcvfs.translatePath(addon.getAddonInfo('profile')), 'warmup_state.json')

def _warmup_state_load():
    # 戻り値：dict＝読めた（{}は「まだ無い＝実績なし」）、None＝ファイルは
    # あるが読めない（状態不明。セーフガードの既定値で走らせる対象）
    import json
    if not os.path.exists(_warmup_state_path()):
        return {}
    try:
        with open(_warmup_state_path(), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def _warmup_state_save(state):
    import json
    try:
        profile_dir = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
        if not os.path.exists(profile_dir):
            os.makedirs(profile_dir)
        state['updated'] = time.time()
        with open(_warmup_state_path(), 'w', encoding='utf-8') as f:
            json.dump(state, f)
    except Exception as e:
        xbmc.log(f"[{addon_name}] UPnP Warmup: failed to save state file: {str(e)}", xbmc.LOGWARNING)

def run_upnp_warmup(monitor):
    """起動時ウォームアップ本体。例外は必ずここで閉じ込め、
    監視サービス本体を巻き込まない"""
    import base64
    import glob
    import itertools
    import re
    import subprocess
    import urllib.error

    try:
        configured_interval = addon.getSettingInt("warmup_interval_ms")
        start_delay = addon.getSettingInt("warmup_start_delay_sec")
        order_label = addon.getSetting("warmup_order")
        order_sql = WARMUP_ORDER_SQL.get(order_label, 'idSong ASC')
        if order_label not in WARMUP_ORDER_SQL:
            order_label = 'ID順（古い順）'
        workers = addon.getSettingInt("warmup_parallel")
        if workers < 1:
            workers = 1
        if workers > 8:
            workers = 8

        # ---- 異常終了ガード：前回が「実行中」のまま消えていたら、
        # そのときの設定値を容疑値とし、今回は実績のある値で走る。
        # ただし、前回の実行が猶予時間（warmup_guard_grace_min、既定10分、
        # コード側で5〜60分に収める）以上続いてから消えた場合は、
        # 値のせいとはみなさず容疑をかけない（途中の再起動などで
        # 間隔の検証が止まらないようにするため）。
        # 実績が無い・状態ファイルが読めない（不明な）場合は、
        # セーフガードの既定値 WARMUP_SAFE_INTERVAL_MS に戻す。
        # 設定値そのものは書き換えない（ユーザーが値を変えたら解除）。
        # 完走した場合は、その間隔（代替値でも）を実績として記録し、
        # 容疑は消す。
        grace_min = addon.getSettingInt("warmup_guard_grace_min")
        if grace_min <= 0:
            grace_min = 10
        grace_min = max(5, min(60, grace_min))
        wu_state = _warmup_state_load()
        state_unknown = wu_state is None
        if state_unknown:
            wu_state = {}
        last_good = wu_state.get('last_good_interval')
        suspect = wu_state.get('suspect_interval')
        if wu_state.get('phase') == 'running':
            prev_elapsed = wu_state.get('elapsed_sec') or 0
            if prev_elapsed >= grace_min * 60:
                xbmc.log(f"[{addon_name}] UPnP Warmup: previous run vanished after {prev_elapsed:.0f}s (grace {grace_min}min) - value not indicted", xbmc.LOGWARNING)
            else:
                suspect = wu_state.get('configured_interval')
                xbmc.log(f"[{addon_name}] UPnP Warmup: previous run did not finish (progress={wu_state.get('progress')}, elapsed={prev_elapsed:.0f}s, configured was {suspect}ms) - guard armed", xbmc.LOGWARNING)
        if suspect is not None and configured_interval != suspect:
            xbmc.log(f"[{addon_name}] UPnP Warmup: interval setting changed ({suspect}ms -> {configured_interval}ms) - guard cleared", xbmc.LOGINFO)
            suspect = None
        guard_active = suspect is not None and configured_interval == suspect
        if state_unknown:
            interval_ms = WARMUP_SAFE_INTERVAL_MS
            xbmc.log(f"[{addon_name}] UPnP Warmup: state file unreadable - using safeguard default {interval_ms}ms for this run", xbmc.LOGWARNING)
            dialog.notification(addon_name, f"UPnPウォームアップ: 状態不明のため、今回だけ間隔{interval_ms}msで実行（設定は変更していません）", xbmcgui.NOTIFICATION_WARNING, 10000)
        elif guard_active:
            interval_ms = last_good if last_good else WARMUP_SAFE_INTERVAL_MS
            xbmc.log(f"[{addon_name}] UPnP Warmup: GUARD ACTIVE - configured {configured_interval}ms is suspect; using {interval_ms}ms for this run", xbmc.LOGWARNING)
            dialog.notification(addon_name, f"UPnPウォームアップ: 前回未完了のため、今回だけ間隔{interval_ms}msで実行（設定は変更していません）", xbmcgui.NOTIFICATION_WARNING, 10000)
        else:
            interval_ms = configured_interval
        xbmc.log(f"[{addon_name}] UPnP Warmup: start (interval={interval_ms}ms, configured={configured_interval}ms, order={order_label}, start_delay={start_delay}s, parallel={workers})", xbmc.LOGINFO)

        if start_delay > 0 and not _upnp_warmup_sleep(monitor, start_delay):
            xbmc.log(f"[{addon_name}] UPnP Warmup: aborted during start delay (Kodi shutdown)", xbmc.LOGINFO)
            return

        # 音楽DB（版数が最も大きいMyMusic*.db＝現行DB）を特定する
        db_abs_path = os.path.abspath(xbmcvfs.translatePath('special://database/'))
        db_files = glob.glob(os.path.join(db_abs_path, 'MyMusic*.db'))
        if not db_files:
            xbmc.log(f"[{addon_name}] UPnP Warmup: no MyMusic*.db found in {db_abs_path} - aborting", xbmc.LOGERROR)
            return
        def _dbver(p):
            m = re.search(r'(\d+)', os.path.basename(p))
            return int(m.group(1)) if m else 0
        music_db = max(db_files, key=_dbver)

        total = 0
        try:
            r = subprocess.run(['sqlite3', music_db, 'SELECT COUNT(*) FROM song;'],
                               capture_output=True, text=True, timeout=60)
            total = int(r.stdout.strip().splitlines()[-1])
        except Exception as e:
            xbmc.log(f"[{addon_name}] UPnP Warmup: COUNT query failed ({str(e)}) - continuing without total", xbmc.LOGWARNING)

        xbmc.log(f"[{addon_name}] UPnP Warmup: DB={os.path.basename(music_db)} total_songs={total}", xbmc.LOGINFO)

        # 追加日順で dateAdded 列が無い版の DB なら、ID降順に落とす
        if order_sql.startswith('dateAdded'):
            try:
                r = subprocess.run(['sqlite3', music_db, 'PRAGMA table_info(song);'],
                                   capture_output=True, text=True, timeout=60)
                cols = [ln.split('|')[1] for ln in r.stdout.splitlines() if '|' in ln]
            except Exception:
                cols = []
            if 'dateAdded' not in cols:
                xbmc.log(f"[{addon_name}] UPnP Warmup: dateAdded column not found in this DB - falling back to idSong DESC order", xbmc.LOGWARNING)
                order_sql = 'idSong DESC'

        sep = '\x1f'
        proc = subprocess.Popen(['sqlite3', '-noheader', '-separator', sep, music_db,
                                 f'SELECT idSong, strFileName FROM song ORDER BY {order_sql};'],
                                stdout=subprocess.PIPE, text=True,
                                encoding='utf-8', errors='replace')

        done = ok = failed = skipped = 0
        consec_fail = 0
        aborted = False
        t_start = time.time()
        try:
            row_iter = iter(proc.stdout)
            first_line = next(row_iter, None)

            # 先頭行の確認：読み取りストリームから曲が1件は取れることだけを見る
            # （制御URLの確定は曲に依存しないプローブで行う）
            first_oid = None
            if first_line:
                p = first_line.rstrip('\n').split(sep)
                if len(p) == 2 and '.' in p[1]:
                    first_oid = base64.b64encode(
                        f"musicdb://songs/{p[0]}.{p[1].rsplit('.', 1)[-1]}".encode('utf-8')).decode('ascii')
            if not first_oid:
                xbmc.log(f"[{addon_name}] UPnP Warmup: no usable track found - aborting", xbmc.LOGERROR)
                return

            candidates, url_source = _upnp_control_url_candidates()
            # 自動取得の場合だけ、記録されたポートが応答しないときに
            # SSDP探索で実際の制御URLを探す（サーバー部品が動的ポートに
            # 移っている場合への対処）。手入力URLはユーザーの指定を優先し、
            # 探索では置き換えない
            server_uuid = _upnp_server_uuid() if url_source.startswith("auto") else ""
            if not candidates and not server_uuid:
                xbmc.log(f"[{addon_name}] UPnP Warmup: no control URL available ({url_source}) - aborting", xbmc.LOGERROR)
                return
            control_url = None
            probe_started = time.time()
            probe_round = 0
            last_probe_errors = {}
            discovered_urls = []

            def _probe_any(urls):
                for cand in urls:
                    try:
                        _upnp_probe_system_update_id(cand)
                        return cand
                    except urllib.error.HTTPError:
                        return cand  # サーバーは応答した＝URLは正しい
                    except Exception as e:
                        err = f"{type(e).__name__}: {str(e)}"
                        if last_probe_errors.get(cand) != err:
                            xbmc.log(f"[{addon_name}] UPnP Warmup: probe failed for {cand}: {err}", xbmc.LOGWARNING)
                            last_probe_errors[cand] = err
                return None

            while not control_url:
                probe_round += 1
                control_url = _probe_any(candidates)
                if not control_url and server_uuid:
                    new_urls = [u for u in _ssdp_discover_control_urls(server_uuid)
                                if u not in candidates]
                    for u in new_urls:
                        candidates.append(u)
                        discovered_urls.append(u)
                        xbmc.log(f"[{addon_name}] UPnP Warmup: SSDP discovery found control URL: {u}", xbmc.LOGINFO)
                    if new_urls:
                        control_url = _probe_any(new_urls)
                if control_url:
                    break
                if time.time() - probe_started > WARMUP_PROBE_MAX_WAIT_SEC:
                    xbmc.log(f"[{addon_name}] UPnP Warmup: UPnP server did not answer within {WARMUP_PROBE_MAX_WAIT_SEC}s - giving up this run", xbmc.LOGERROR)
                    break
                xbmc.log(f"[{addon_name}] UPnP Warmup: waiting for UPnP server (round {probe_round}, elapsed {time.time() - probe_started:.0f}s)", xbmc.LOGINFO)
                if not _upnp_warmup_sleep(monitor, 30):
                    xbmc.log(f"[{addon_name}] UPnP Warmup: aborted while connecting (Kodi shutdown)", xbmc.LOGINFO)
                    return
            if not control_url:
                xbmc.log(f"[{addon_name}] UPnP Warmup: no control URL answered ({url_source}) - aborting", xbmc.LOGERROR)
                return
            if control_url in discovered_urls:
                url_source = "SSDP discovery"
            xbmc.log(f"[{addon_name}] UPnP Warmup: control URL = {control_url} (source: {url_source})", xbmc.LOGINFO)

            # ここから本走行。「実行中」マーカーを残し、正常終了しなければ
            # 次回起動時にガードが検知できるようにする
            t_run = time.time()
            wu_state = {'phase': 'running', 'interval_used': interval_ms,
                        'configured_interval': configured_interval, 'workers': workers, 'progress': 0,
                        'elapsed_sec': 0,
                        'last_good_interval': last_good, 'suspect_interval': suspect}
            _warmup_state_save(wu_state)
            completed = False

            # まず全曲分の ObjectID を組み立てる（DBの読み切りは数秒）。
            # そのうえで、要求の送信だけをワーカースレッドで並列化する。
            # 並列数1なら、従来と同じ動きになる
            items = []
            for line in itertools.chain([first_line], row_iter):
                line = line.rstrip('\n')
                if not line:
                    continue
                parts = line.split(sep)
                if len(parts) != 2 or '.' not in parts[1]:
                    skipped += 1
                    continue
                id_song, file_name = parts
                ext = file_name.rsplit('.', 1)[-1]
                oid = base64.b64encode(
                    f"musicdb://songs/{id_song}.{ext}".encode('utf-8')).decode('ascii')
                items.append((id_song, oid))

            # 並列実行の共有状態。カウンタ類はロックで守る
            lock = threading.Lock()
            stop_event = threading.Event()
            cnt = {'ok': 0, 'failed': 0, 'done': 0, 'consec': 0}
            next_pos = [0]
            last_milestone = [0]

            def _wait(sec):
                # 打ち切り（Kodi終了・連続失敗での中止）に応答する小刻みな待機
                end = time.time() + sec
                while time.time() < end:
                    if stop_event.is_set() or monitor.abortRequested():
                        return False
                    time.sleep(min(0.25, end - time.time()))
                return not (stop_event.is_set() or monitor.abortRequested())

            def _worker():
                while not stop_event.is_set() and not monitor.abortRequested():
                    with lock:
                        pos = next_pos[0]
                        next_pos[0] = pos + 10
                    if pos >= len(items):
                        return
                    for id_song, oid in items[pos:pos + 10]:
                        if stop_event.is_set() or monitor.abortRequested():
                            return
                        # 1曲分。接続エラー（UPnPサーバー未起動など）は
                        # 15秒待って同じ曲を再試行する（最大8回）
                        retries = 0
                        while True:
                            try:
                                _upnp_browse_metadata_once(control_url, oid)
                                with lock:
                                    cnt['ok'] += 1
                                    cnt['consec'] = 0
                                break
                            except urllib.error.HTTPError as e:
                                with lock:
                                    cnt['failed'] += 1
                                    cnt['consec'] += 1
                                    nfail = cnt['failed']
                                if nfail <= 5:
                                    xbmc.log(f"[{addon_name}] UPnP Warmup: track failed idSong={id_song}: HTTP {e.code}", xbmc.LOGWARNING)
                                break
                            except urllib.error.URLError:
                                retries += 1
                                if retries > 8:
                                    with lock:
                                        cnt['failed'] += 1
                                        cnt['consec'] += 1
                                    xbmc.log(f"[{addon_name}] UPnP Warmup: UPnP server unreachable, giving up on idSong={id_song}", xbmc.LOGWARNING)
                                    break
                                xbmc.log(f"[{addon_name}] UPnP Warmup: UPnP server not reachable yet (retry {retries}/8), waiting 15s", xbmc.LOGWARNING)
                                if not _wait(15):
                                    return
                            except Exception as e:
                                with lock:
                                    cnt['failed'] += 1
                                    cnt['consec'] += 1
                                    nfail = cnt['failed']
                                if nfail <= 5:
                                    xbmc.log(f"[{addon_name}] UPnP Warmup: track failed idSong={id_song}: {str(e)}", xbmc.LOGWARNING)
                                break
                        with lock:
                            cnt['done'] += 1
                            done_now = cnt['done']
                            consec_now = cnt['consec']
                            ok_now = cnt['ok']
                            failed_now = cnt['failed']
                            milestone = done_now // 500
                            do_log = milestone > last_milestone[0]
                            if do_log:
                                last_milestone[0] = milestone
                        if consec_now >= 20:
                            xbmc.log(f"[{addon_name}] UPnP Warmup: 20 consecutive failures - aborting this run", xbmc.LOGERROR)
                            stop_event.set()
                            return
                        if do_log:
                            xbmc.log(f"[{addon_name}] UPnP Warmup: progress {done_now}/{total} ok={ok_now} failed={failed_now} elapsed={time.time() - t_start:.0f}s", xbmc.LOGINFO)
                            wu_state['progress'] = done_now
                            wu_state['elapsed_sec'] = time.time() - t_run
                            _warmup_state_save(wu_state)
                        if interval_ms > 0:
                            if not _wait(interval_ms / 1000.0):
                                return

            threads = []
            for _ in range(workers):
                th = threading.Thread(target=_worker, daemon=True)
                th.start()
                threads.append(th)
            for th in threads:
                th.join()
            ok = cnt['ok']
            failed = cnt['failed']
            done = cnt['done']
            if monitor.abortRequested():
                aborted = True
            completed = (done >= len(items)) and not aborted and not stop_event.is_set()
        finally:
            if proc.poll() is None:
                proc.terminate()

        xbmc.log(f"[{addon_name}] UPnP Warmup: {'ABORTED' if aborted else 'DONE'} processed={done} ok={ok} failed={failed} skipped={skipped} elapsed={time.time() - t_start:.0f}s", xbmc.LOGINFO)
        # 状態ファイルの締め：完走なら正常値を更新、正常終了なら stopped、
        # 失敗打ち切りなら failed（異常終了ガードの対象にはしない）
        if completed:
            # 完走した間隔は、設定値でも代替値でも実績として記録し、容疑は消す
            if suspect is not None or last_good != interval_ms:
                xbmc.log(f"[{addon_name}] UPnP Warmup: completion recorded - last_good={interval_ms}ms, suspicion cleared", xbmc.LOGINFO)
            last_good = interval_ms
            suspect = None
            wu_state.update({'phase': 'done', 'progress': done,
                             'last_good_interval': last_good, 'suspect_interval': suspect})
        elif aborted:
            wu_state.update({'phase': 'stopped', 'progress': done,
                             'last_good_interval': last_good, 'suspect_interval': suspect})
        else:
            wu_state.update({'phase': 'failed', 'progress': done,
                             'last_good_interval': last_good, 'suspect_interval': suspect})
        _warmup_state_save(wu_state)
        if not aborted:
            dialog.notification(addon_name, f"UPnPウォームアップ完了（{ok}曲）", xbmcgui.NOTIFICATION_INFO, 5000)
    except Exception as e:
        xbmc.log(f"[{addon_name}] UPnP Warmup: unexpected error: {str(e)}", xbmc.LOGERROR)

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
        if addon.getSettingBool("enable_upnp_warmup"):
            xbmc.log(f"[{addon_name}] UPnP Warmup: ENABLED in settings - starting background thread", xbmc.LOGINFO)
            _wu_thread = threading.Thread(target=run_upnp_warmup, args=(monitor,))
            _wu_thread.daemon = True
            _wu_thread.start()
        else:
            xbmc.log(f"[{addon_name}] UPnP Warmup: disabled in settings", xbmc.LOGINFO)
        while not monitor.waitForAbort(1):
            # 毎秒タイマーの代わりとなる実行チェックを行う
            monitor.check_and_execute()
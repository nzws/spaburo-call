import asyncio
import logging
import re
from typing import Optional

import httpx
import pjsua2 as pj

from call_session import CallerInfo, CallSession, SessionConfig, SessionDeps
from media import PjCallControl, configure_media
from utils.call_logger import CallLogger
from utils.transcribe import transcribe
from utils.webhook import check_spam


def get_sip_header(whole_msg: str, name: str) -> Optional[str]:
    """SIPメッセージからヘッダーを取得"""
    # 1行ヘッダ前提（折り返しが来るなら拡張が必要）
    m = re.search(rf"(?im)^{re.escape(name)}\s*:\s*(.+?)\r?$", whole_msg)
    return m.group(1).strip() if m else None


def extract_number(uri: str) -> str:
    """SIP URIから電話番号を抽出"""
    # 例: "<sip:+819012345678@domain>" -> "+819012345678"
    match = re.search(r"sip:([^@>]+)@", uri)
    return match.group(1) if match else "unknown"


class MyCall(pj.Call):
    """薄い着信Call。イベントをasyncio.Eventへ転送するだけ"""

    def __init__(self, account: pj.Account, call_id: int):
        super().__init__(account, call_id)
        self.media_active = asyncio.Event()
        self.media_lost = asyncio.Event()
        self.confirmed = asyncio.Event()
        self.term = asyncio.Event()
        self.audio_media: Optional[pj.AudioMedia] = None
        self.last_status_code = 0
        self.on_terminated = None  # SipBotがレジストリ掃除用に設定する
        self.logger = logging.getLogger("MyCall")

    def onCallState(self, prm: pj.OnCallStateParam) -> None:
        ci = self.getInfo()
        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.confirmed.set()
        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self.last_status_code = ci.lastStatusCode
            self.logger.info(
                f"通話が終了しました: {ci.remoteUri} (status={ci.lastStatusCode})"
            )
            self.term.set()
            if self.on_terminated:
                self.on_terminated(self)

    def onCallMediaState(self, prm: pj.OnCallMediaStateParam) -> None:
        ci = self.getInfo()
        has_active_audio = False
        for mi in ci.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                self.audio_media = self.getAudioMedia(mi.index)
                has_active_audio = True
        if has_active_audio:
            self.media_active.set()
        elif self.media_active.is_set():
            # hold/re-INVITE等でメディアが失われた（録音側はこれで終了する）
            self.media_lost.set()


class MyAccount(pj.Account):
    """SIPアカウント。着信時にCallSessionタスクを起動する"""

    def __init__(self, bot: "SipBot"):
        super().__init__()
        self.bot = bot
        self.logger = logging.getLogger("MyAccount")

    def onRegState(self, prm: pj.OnRegStateParam) -> None:
        info = self.getInfo()
        if info.regIsActive and 200 <= prm.code < 300:
            self.logger.info(
                f"SIP登録に成功しました: code={prm.code} reason={prm.reason} "
                f"expiration={prm.expiration}秒"
            )
        else:
            self.logger.error(
                f"SIP登録に失敗しました: code={prm.code} reason={prm.reason}"
            )

    def onIncomingCall(self, prm: pj.OnIncomingCallParam) -> None:
        if self.bot.stopping:
            self.logger.info("シャットダウン中のため着信を無視します")
            return

        pai = get_sip_header(prm.rdata.wholeMsg, "P-Asserted-Identity")
        call = MyCall(self, prm.callId)
        ci = call.getInfo()
        caller = CallerInfo(
            from_number=extract_number(ci.remoteUri),
            p_asserted_identity=pai,
            to_number=extract_number(ci.localUri),
        )
        self.logger.info(
            f"着信を受信しました: from={caller.from_number}, "
            f"pai={caller.p_asserted_identity}, to={caller.to_number}"
        )
        self.bot.launch_session(call, caller)


class SipBot:
    """SIPボットのメインクラス

    start()/shutdown()/destroy()/libHandleEvents()はすべて同一スレッド
    （asyncioイベントループのスレッド）から呼ぶこと。
    """

    def __init__(
        self,
        sip_domain: str,
        sip_user: str,
        sip_password: str,
        session_config: SessionConfig,
        webhook_url: Optional[str] = None,
        sip_auth_user: Optional[str] = None,
        groq_api_key: Optional[str] = None,
        transcribe_model: str = "whisper-large-v3-turbo",
        mqtt_broker: Optional[str] = None,
        mqtt_port: int = 1883,
        mqtt_topic: Optional[str] = None,
        mqtt_username: Optional[str] = None,
        mqtt_password: Optional[str] = None,
    ):
        self.sip_domain = sip_domain
        self.sip_user = sip_user
        self.sip_auth_user = sip_auth_user or sip_user
        self.sip_password = sip_password
        self.webhook_url = webhook_url
        self.session_config = session_config
        self.groq_api_key = groq_api_key
        self.transcribe_model = transcribe_model
        self.logger = logging.getLogger("SipBot")
        self.stopping = False

        self.call_logger = CallLogger(
            broker=mqtt_broker,
            port=mqtt_port,
            topic=mqtt_topic,
            username=mqtt_username,
            password=mqtt_password,
        )
        self.http = httpx.AsyncClient()  # プロセス単位で再利用
        self.ep: Optional[pj.Endpoint] = None
        self.account: Optional[MyAccount] = None
        # id(call) -> (MyCall, asyncio.Task)
        # 削除は「タスク完了 AND DISCONNECTED」の両方が揃ったときのみ
        self.sessions: dict[int, tuple[MyCall, asyncio.Task]] = {}

    # ---- 起動 ----

    def start(self) -> None:
        """PJSIPを初期化して登録する（イベントループのスレッドで呼ぶこと）"""
        self.ep = pj.Endpoint()
        self.ep.libCreate()

        ep_cfg = pj.EpConfig()
        # asyncio統合: スレッドを作らせず、libHandleEvents()で駆動する
        ep_cfg.uaConfig.threadCnt = 0
        ep_cfg.uaConfig.mainThreadOnly = True
        ep_cfg.medConfig.clockRate = 8000
        ep_cfg.medConfig.sndClockRate = 8000
        ep_cfg.medConfig.channelCount = 1
        debug_enabled = logging.getLogger().isEnabledFor(logging.DEBUG)
        pj_log_level = 5 if debug_enabled else 3
        ep_cfg.logConfig.level = pj_log_level
        ep_cfg.logConfig.consoleLevel = pj_log_level
        self.ep.libInit(ep_cfg)

        transport_cfg = pj.TransportConfig()
        transport_cfg.port = 0
        self.ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, transport_cfg)

        self.ep.libStart()
        configure_media(self.ep)
        self.logger.info("PJSIP Endpointを起動しました")

        self._register_account()

    def _register_account(self) -> None:
        acc_cfg = pj.AccountConfig()
        acc_cfg.idUri = f"sip:{self.sip_user}@{self.sip_domain}"
        acc_cfg.regConfig.registrarUri = f"sip:{self.sip_domain}"
        # NAT越しのため登録有効期限を短めにし、NATテーブルの失効による着信不達を防ぐ
        acc_cfg.regConfig.timeoutSec = 300

        cred = pj.AuthCredInfo()
        cred.scheme = "digest"
        cred.realm = "*"
        cred.username = self.sip_auth_user
        cred.dataType = pj.PJSIP_CRED_DATA_PLAIN_PASSWD
        cred.data = self.sip_password
        acc_cfg.sipConfig.authCreds.append(cred)

        self.account = MyAccount(self)
        self.account.create(acc_cfg)
        self.logger.info(
            f"SIP登録要求を送信しました: {acc_cfg.idUri} -> {acc_cfg.regConfig.registrarUri}"
        )

    # ---- セッション管理 ----

    def launch_session(self, call: MyCall, caller: CallerInfo) -> None:
        """着信コールバックから呼ばれ、CallSessionタスクを起動する"""

        async def check(c: CallerInfo):
            return await check_spam(
                self.http, self.webhook_url, c.from_number, c.to_number, c.p_asserted_identity
            )

        async def do_transcribe(wav_path: str):
            return await transcribe(self.http, self.groq_api_key, self.transcribe_model, wav_path)

        deps = SessionDeps(check_spam=check, transcribe=do_transcribe, log=self.call_logger.log)
        session = CallSession(PjCallControl(call), caller, self.session_config, deps)
        loop = asyncio.get_running_loop()
        task = loop.create_task(session.run())

        call_id = id(call)
        self.sessions[call_id] = (call, task)

        def cleanup() -> None:
            # タスク完了 AND DISCONNECTED が揃ったときのみCall参照を解放する。
            # 正常着信(留守電OFF)ではタスクが先に終わるが、電話機が鳴っている間は
            # Callを保持し続け、DISCONNECTEDコールバックを受けられるようにする。
            entry = self.sessions.get(call_id)
            if entry is None:
                return
            entry_call, entry_task = entry
            if entry_task.done() and entry_call.term.is_set():
                self.sessions.pop(call_id, None)

        # pjsua2コールバック内でのCall破棄を避けるため、削除はcall_soonで遅延させる
        call.on_terminated = lambda _c: loop.call_soon(cleanup)
        task.add_done_callback(lambda _t: loop.call_soon(cleanup))

    # ---- 停止 ----

    async def shutdown(self) -> None:
        """設計§4.1の順序でシャットダウンする（libDestroyは呼ばない: destroy()で行う）"""
        self.logger.info("SIPボットを停止します...")
        self.stopping = True  # 1. 新規着信の受付停止

        # 2a. アクティブな通話を切断してセッションを自然完了させる
        #     （録音中のセッションは終端を検知→録音確定→通知まで自力で進む）
        for call, _task in list(self.sessions.values()):
            if not call.term.is_set():
                try:
                    call.hangup(pj.CallOpParam())
                except pj.Error as e:
                    self.logger.info(f"シャットダウン切断で競合しました（正常）: {e.info()}")

        # 2b. セッションの完了を待ち、終わらないものだけキャンセルする
        tasks = [task for _call, task in self.sessions.values()]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=15)
            for t in pending:
                t.cancel()
            if pending:
                # キャンセル時もCallSessionのfinallyが録音確定・通知を行う
                _done2, still_pending = await asyncio.wait(pending, timeout=10)
                if still_pending:
                    self.logger.critical(
                        f"シャットダウン: {len(still_pending)}件のセッションタスクが終了しませんでした"
                        "（再キャンセルして追加で待機します）"
                    )
                    for t in still_pending:
                        t.cancel()
                    # キャンセルは協調的であり、2回のキャンセルを生き延びるタスクはバグである。
                    # それでも無限待機はしない: シャットダウンを永久に止める方が害が大きいため。
                    _done3, still_pending2 = await asyncio.wait(still_pending, timeout=5)
                    if still_pending2:
                        self.logger.critical(
                            f"シャットダウン: {len(still_pending2)}件のセッションタスクが"
                            "再キャンセル後も終了しませんでした。処理を続行します"
                        )

        # 3. MQTT flush（voicemail_recorded等の送信完了を待つ）
        flushed = await asyncio.to_thread(self.call_logger.flush)
        if not flushed:
            self.logger.error("MQTTログのflushが完了しませんでした（通知が失われた可能性）")
        await self.http.aclose()

        # 4. Account shutdown
        if self.account:
            try:
                self.account.shutdown()
            except pj.Error as e:
                self.logger.error(f"アカウントのシャットダウンに失敗: {e.info()}")

    def destroy(self) -> None:
        """5. libDestroy（ポンプ停止後に同一スレッドで呼ぶ）"""
        if self.ep:
            try:
                self.ep.libDestroy()
            except pj.Error as e:
                self.logger.error(f"Endpointの破棄に失敗: {e.info()}")
        self.logger.info("SIPボットを停止しました")

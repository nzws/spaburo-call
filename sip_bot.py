import logging
import re
from dataclasses import dataclass
from typing import Optional

import pjsua2 as pj

from utils.call_logger import CallLogger
from utils.webhook import check_spam


def get_sip_header(whole_msg: str, name: str) -> Optional[str]:
    """SIPメッセージからヘッダーを取得"""
    # 1行ヘッダ前提（折り返しが来るなら拡張が必要）
    m = re.search(rf"(?im)^{re.escape(name)}\s*:\s*(.+?)\r?$", whole_msg)
    return m.group(1).strip() if m else None


@dataclass
class CallerInfo:
    """発信者情報"""

    from_number: str
    p_asserted_identity: Optional[str]
    to_number: str


class MyCall(pj.Call):
    """着信コールを処理するクラス"""

    def __init__(
        self,
        account: pj.Account,
        call_id: int,
        webhook_url: Optional[str],
        call_logger: CallLogger,
        auto_block_enabled: bool,
        p_asserted_identity: Optional[str] = None,
    ):
        super().__init__(account, call_id)
        self.webhook_url = webhook_url
        self.call_logger = call_logger
        self.auto_block_enabled = auto_block_enabled
        self.p_asserted_identity = p_asserted_identity
        self.logger = logging.getLogger("MyCall")
        self.should_hangup = False  # 切断フラグ

    def onCallState(self, prm: pj.OnCallStateParam) -> None:
        """着信状態変化時のコールバック"""
        ci = self.getInfo()

        if ci.state == pj.PJSIP_INV_STATE_INCOMING:
            self.handle_incoming_call(ci)
        elif ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            # 通話確立状態
            if self.should_hangup:
                self.logger.info("スパム電話を切断しました")
                self.hangup(pj.CallOpParam())
        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self.logger.info(f"通話が切断されました: {ci.remoteUri}")

    def handle_incoming_call(self, ci: pj.CallInfo) -> None:
        """着信処理"""
        # CallerIDを抽出
        caller_info = self.extract_caller_info(ci)

        self.logger.info(
            f"着信を受信しました: from={caller_info.from_number}, "
            f"pai={caller_info.p_asserted_identity}, to={caller_info.to_number}"
        )

        # ログ送信: received
        self.call_logger.log(
            action="received",
            from_number=caller_info.from_number,
            p_asserted_identity=caller_info.p_asserted_identity,
            to_number=caller_info.to_number,
        )

        # Webhookでスパム判定
        is_spam, reason = check_spam(
            self.webhook_url,
            caller_info.from_number,
            caller_info.to_number,
            caller_info.p_asserted_identity,
        )

        self.logger.info(
            f"スパム判定結果: caller={caller_info.from_number}, "
            f"is_spam={is_spam}, reason={reason}"
        )

        # ログ送信: spam_detected or legitimate
        self.call_logger.log(
            action="spam_detected" if is_spam else "legitimate",
            from_number=caller_info.from_number,
            p_asserted_identity=caller_info.p_asserted_identity,
            to_number=caller_info.to_number,
            reason=reason,
        )

        # 自動ブロックが有効な場合、スパムなら即応答→即切断
        if self.auto_block_enabled and is_spam:
            self.logger.info(f"スパム電話をブロックします: {caller_info.from_number}")

            # ログ送信: blocked
            self.call_logger.log(
                action="blocked",
                from_number=caller_info.from_number,
                p_asserted_identity=caller_info.p_asserted_identity,
                to_number=caller_info.to_number,
            )

            # 応答（200 OK）を返す
            call_op_param = pj.CallOpParam()
            call_op_param.statusCode = pj.PJSIP_SC_OK
            self.answer(call_op_param)

            # 切断フラグを立てる
            self.should_hangup = True
        else:
            # スパムでない場合は応答しない
            self.logger.info(f"着信を無視します: {caller_info.from_number}")
            # 何もしない（INVITEに応答しない）

    def extract_caller_info(self, ci: pj.CallInfo) -> CallerInfo:
        """CallerID情報を抽出"""
        # Fromヘッダーから番号を抽出
        from_number = self.extract_number(ci.remoteUri)

        # P-Asserted-Identityは onIncomingCall で取得済み
        p_asserted_identity = self.p_asserted_identity

        # To番号
        to_number = self.extract_number(ci.localUri)

        return CallerInfo(
            from_number=from_number,
            p_asserted_identity=p_asserted_identity,
            to_number=to_number,
        )

    @staticmethod
    def extract_number(uri: str) -> str:
        """SIP URIから電話番号を抽出"""
        # 例: "sip:+819012345678@domain" -> "+819012345678"
        # 例: "<sip:09012345678@domain>" -> "09012345678"
        match = re.search(r"sip:([^@>]+)@", uri)
        return match.group(1) if match else "unknown"


class MyAccount(pj.Account):
    """SIPアカウントクラス"""

    def __init__(
        self,
        webhook_url: Optional[str],
        call_logger: CallLogger,
        auto_block_enabled: bool,
    ):
        super().__init__()
        self.webhook_url = webhook_url
        self.call_logger = call_logger
        self.auto_block_enabled = auto_block_enabled
        self.logger = logging.getLogger("MyAccount")
        self.calls = []  # Callオブジェクトを保持するリスト

    def onRegState(self, prm: pj.OnRegStateParam) -> None:
        """SIP登録状態変化時のコールバック"""
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
        """着信時のコールバック"""
        # P-Asserted-Identityを取得
        whole_msg = prm.rdata.wholeMsg
        pai = get_sip_header(whole_msg, "P-Asserted-Identity")
        if pai:
            self.logger.debug(f"P-Asserted-Identity: {pai}")

        call = MyCall(
            self,
            prm.callId,
            self.webhook_url,
            self.call_logger,
            self.auto_block_enabled,
            p_asserted_identity=pai,
        )
        self.calls.append(call)  # Callオブジェクトを保持


class SipBot:
    """SIPボットのメインクラス"""

    def __init__(
        self,
        sip_domain: str,
        sip_user: str,
        sip_password: str,
        auto_block_enabled: bool,
        webhook_url: Optional[str] = None,
        sip_auth_user: Optional[str] = None,
        mqtt_broker: Optional[str] = None,
        mqtt_port: int = 1883,
        mqtt_topic: Optional[str] = None,
        mqtt_username: Optional[str] = None,
        mqtt_password: Optional[str] = None,
    ):
        self.sip_domain = sip_domain
        self.sip_user = sip_user
        self.sip_auth_user = sip_auth_user or sip_user  # 未設定時はsip_userを使用
        self.sip_password = sip_password
        self.auto_block_enabled = auto_block_enabled
        self.webhook_url = webhook_url
        self.logger = logging.getLogger("SipBot")

        self.call_logger = CallLogger(
            broker=mqtt_broker,
            port=mqtt_port,
            topic=mqtt_topic,
            username=mqtt_username,
            password=mqtt_password,
        )

        self.ep: Optional[pj.Endpoint] = None
        self.account: Optional[MyAccount] = None

    def start(self) -> None:
        """SIPボットを起動"""
        try:
            # Endpointを作成
            self.ep = pj.Endpoint()
            self.ep.libCreate()

            # Endpoint設定
            ep_cfg = pj.EpConfig()
            # PJSIPのログレベルをPythonのログレベルに連動させる
            # （DEBUG時は5=詳細トレース、それ以外は3=INFO相当）
            debug_enabled = logging.getLogger().isEnabledFor(logging.DEBUG)
            pj_log_level = 5 if debug_enabled else 3
            ep_cfg.logConfig.level = pj_log_level
            ep_cfg.logConfig.consoleLevel = pj_log_level
            self.ep.libInit(ep_cfg)

            # トランスポート設定（UDP）
            transport_cfg = pj.TransportConfig()
            transport_cfg.port = 0  # 自動割り当て
            self.ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, transport_cfg)

            # Endpointを開始
            self.ep.libStart()
            self.logger.info("PJSIP Endpointを起動しました")

            # アカウントを作成・登録
            self.register_account()

        except Exception as e:
            self.logger.error(f"起動に失敗しました: {e}", exc_info=True)
            raise

    def register_account(self) -> None:
        """SIPアカウントを登録"""
        # アカウント設定
        acc_cfg = pj.AccountConfig()
        acc_cfg.idUri = f"sip:{self.sip_user}@{self.sip_domain}"
        acc_cfg.regConfig.registrarUri = f"sip:{self.sip_domain}"
        # NAT越しのため登録有効期限を短めにし、NATテーブルの失効による着信不達を防ぐ
        acc_cfg.regConfig.timeoutSec = 300

        # 認証設定
        cred = pj.AuthCredInfo()
        cred.scheme = "digest"
        cred.realm = "*"
        cred.username = self.sip_auth_user  # 認証IDを使用
        cred.dataType = pj.PJSIP_CRED_DATA_PLAIN_PASSWD
        cred.data = self.sip_password
        acc_cfg.sipConfig.authCreds.append(cred)

        # アカウントを作成
        self.account = MyAccount(
            self.webhook_url, self.call_logger, self.auto_block_enabled
        )
        self.account.create(acc_cfg)

        self.logger.info(
            f"SIP登録要求を送信しました（結果はonRegStateで通知）: "
            f"{acc_cfg.idUri} -> {acc_cfg.regConfig.registrarUri}"
        )
        self.logger.info(
            f"認証情報: ユーザーID={self.sip_user}, 認証ID={self.sip_auth_user}"
        )

    def stop(self) -> None:
        """SIPボットを停止"""
        self.logger.info("SIPボットを停止します...")

        if self.account:
            try:
                self.account.shutdown()
            except Exception as e:
                self.logger.error(f"アカウントのシャットダウンに失敗: {e}")

        if self.ep:
            try:
                self.ep.libDestroy()
            except Exception as e:
                self.logger.error(f"Endpointの破棄に失敗: {e}")

        self.logger.info("SIPボットを停止しました")

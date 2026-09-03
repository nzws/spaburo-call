import json
import logging
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt


class CallLogger:
    """通話ログをMQTTで送信"""

    def __init__(
        self,
        broker: Optional[str] = None,
        port: int = 1883,
        topic: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 5,
    ):
        self.broker = broker
        self.port = port
        self.topic = topic
        self.username = username
        self.password = password
        self.timeout = timeout
        self.logger = logging.getLogger("CallLogger")
        self._threads: list[threading.Thread] = []
        self._threads_lock = threading.Lock()

    def log(
        self,
        action: str,
        from_number: str,
        p_asserted_identity: Optional[str],
        to_number: str,
        reason: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> None:
        """ログを送信（非同期）"""
        if not self.broker or not self.topic:
            self.logger.debug("MQTTが設定されていないため、ログを送信しません")
            return

        # 非同期で送信（メインスレッドをブロックしない）
        thread = threading.Thread(
            target=self._send_log,
            args=(action, from_number, p_asserted_identity, to_number, reason, extra),
            daemon=True,
        )
        with self._threads_lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def flush(self, timeout: float = 10.0) -> bool:
        """送信中のログスレッドの完了を待つ（シャットダウン時用）

        Returns:
            全スレッドが完了していればTrue。タイムアウト後も生存しているスレッドが
            あればFalse（通知が失われる可能性がある）。
        """
        deadline = time.monotonic() + timeout
        with self._threads_lock:
            threads = list(self._threads)
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        alive = [t for t in threads if t.is_alive()]
        if alive:
            self.logger.error(
                f"MQTT送信スレッド{len(alive)}件がflushタイムアウト後も未完了です（通知が失われる可能性）"
            )
            return False
        return True

    def _send_log(
        self,
        action: str,
        from_number: str,
        p_asserted_identity: Optional[str],
        to_number: str,
        reason: Optional[str],
        extra: Optional[dict],
    ) -> None:
        """ログを送信（内部実装）"""
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "action": action,
            "from": from_number,
            "p_asserted_identity": p_asserted_identity,
            "to": to_number,
        }

        if reason:
            log_data["reason"] = reason
        if extra:
            log_data.update(extra)

        client = None
        connected = False

        def on_connect(client, userdata, flags, rc):
            nonlocal connected
            if rc == 0:
                connected = True
                self.logger.debug("MQTTに接続しました")
            else:
                self.logger.error(f"MQTT接続に失敗しました: rc={rc}")

        try:
            client = mqtt.Client()
            client.on_connect = on_connect

            # 認証設定（オプション）
            if self.username and self.password:
                client.username_pw_set(self.username, self.password)

            # ネットワークループを先に開始
            client.loop_start()

            # 非同期で接続
            client.connect_async(self.broker, self.port, keepalive=60)

            # 接続完了を待つ（タイムアウト付き）
            wait_time = 0
            while not connected and wait_time < self.timeout:
                time.sleep(0.1)
                wait_time += 0.1

            if not connected:
                self.logger.error(
                    f"MQTT接続がタイムアウトしました: {self.broker}:{self.port}"
                )
                return

            # メッセージ送信（QoS=1で確認応答を待つ）
            payload = json.dumps(log_data, ensure_ascii=False)
            result = client.publish(self.topic, payload, qos=1)
            result.wait_for_publish(timeout=self.timeout)

            # wait_for_publishはタイムアウト時も正常returnするためis_publishedで確認する
            if result.rc == mqtt.MQTT_ERR_SUCCESS and result.is_published():
                self.logger.info(f"ログを送信しました: action={action}, from={from_number}")
            else:
                self.logger.error(
                    f"MQTT送信が完了しませんでした: rc={result.rc}, "
                    f"published={result.is_published()}"
                )

        except socket.timeout:
            self.logger.error(f"MQTT接続がタイムアウトしました: {self.broker}:{self.port}")
        except Exception as e:
            self.logger.error(f"ログの送信に失敗しました: {e}")
        finally:
            if client:
                try:
                    client.loop_stop()
                    client.disconnect()
                except:
                    pass

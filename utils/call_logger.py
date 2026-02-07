import json
import logging
import socket
import threading
import time
from datetime import datetime
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

    def log(
        self,
        action: str,
        from_number: str,
        p_asserted_identity: Optional[str],
        to_number: str,
        reason: Optional[str] = None,
    ) -> None:
        """ログを送信（非同期）"""
        if not self.broker or not self.topic:
            self.logger.debug("MQTTが設定されていないため、ログを送信しません")
            return

        # 非同期で送信（メインスレッドをブロックしない）
        thread = threading.Thread(
            target=self._send_log,
            args=(action, from_number, p_asserted_identity, to_number, reason),
            daemon=True,
        )
        thread.start()

    def _send_log(
        self,
        action: str,
        from_number: str,
        p_asserted_identity: Optional[str],
        to_number: str,
        reason: Optional[str],
    ) -> None:
        """ログを送信（内部実装）"""
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "action": action,
            "from": from_number,
            "p_asserted_identity": p_asserted_identity,
            "to": to_number,
        }

        if reason:
            log_data["reason"] = reason

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

            # メッセージ送信（QoS=0で確認応答を待たない）
            payload = json.dumps(log_data)
            result = client.publish(self.topic, payload, qos=0)

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.logger.info(f"ログを送信しました: action={action}, from={from_number}")
            else:
                self.logger.error(f"MQTT送信に失敗しました: rc={result.rc}")

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

import json
import time
from unittest.mock import MagicMock, patch

from utils.call_logger import CallLogger


def make_logger():
    return CallLogger(broker="mqtt.test", port=1883, topic="t/logs")


def run_with_mock_mqtt(fn, published_ok=True):
    """mqtt.Clientをモックして即接続成功させ、publishを記録する

    Returns:
        (published, infos): publishされた(topic, payload, qos)のリストと
        publishが返したMQTTMessageInfoモックのリスト
    """
    published = []
    infos = []
    with patch("utils.call_logger.mqtt.Client") as client_cls:
        client = MagicMock()
        client_cls.return_value = client

        def connect_async(*args, **kwargs):
            client.on_connect(client, None, None, 0)

        client.connect_async.side_effect = connect_async

        def publish(topic, payload, qos):
            published.append((topic, json.loads(payload), qos))
            info = MagicMock()
            info.rc = 0
            info.is_published.return_value = published_ok
            infos.append(info)
            return info

        client.publish.side_effect = publish
        fn()
    return published, infos


def test_log_publishes_qos1_with_extra():
    logger = make_logger()

    def go():
        logger.log(
            action="voicemail_recorded",
            from_number="0312345678",
            p_asserted_identity=None,
            to_number="0398765432",
            reason="webhook: forced voicemail",
            extra={"duration_sec": 42, "transcription": "もしもし"},
        )
        logger.flush()

    published, infos = run_with_mock_mqtt(go)
    assert len(published) == 1
    topic, payload, qos = published[0]
    assert topic == "t/logs"
    assert qos == 1
    assert payload["action"] == "voicemail_recorded"
    assert payload["duration_sec"] == 42
    assert payload["transcription"] == "もしもし"
    assert payload["reason"] == "webhook: forced voicemail"
    # QoS1の完了確認をしていること
    assert infos[0].wait_for_publish.called
    assert infos[0].is_published.called


def test_log_without_extra_keeps_existing_shape():
    logger = make_logger()

    def go():
        logger.log("received", "03", None, "06")
        logger.flush()

    published, _ = run_with_mock_mqtt(go)
    payload = published[0][1]
    assert set(payload.keys()) == {"timestamp", "action", "from", "p_asserted_identity", "to"}


def test_log_without_broker_is_noop():
    logger = CallLogger()
    logger.log("received", "03", None, "06")  # 例外なし
    logger.flush()


def test_publish_timeout_is_logged_not_raised():
    logger = make_logger()

    def go():
        logger.log("received", "03", None, "06")
        logger.flush()

    # is_published()=False（タイムアウト相当）でも例外にならないこと
    run_with_mock_mqtt(go, published_ok=False)


def test_flush_waits_for_threads():
    logger = make_logger()

    def go():
        for _ in range(5):
            logger.log("received", "03", None, "06")
        result = logger.flush()
        assert result is True
        assert all(not t.is_alive() for t in logger._threads)

    run_with_mock_mqtt(go)


def test_flush_returns_false_and_logs_error_when_thread_still_alive(caplog):
    """publishが遅延して flush のタイムアウトを超えた場合、Falseを返しエラーログを出す"""
    logger = make_logger()

    published = []
    with patch("utils.call_logger.mqtt.Client") as client_cls:
        client = MagicMock()
        client_cls.return_value = client

        def connect_async(*args, **kwargs):
            client.on_connect(client, None, None, 0)

        client.connect_async.side_effect = connect_async

        def publish(topic, payload, qos):
            time.sleep(1.0)  # flushのtimeout(0.1s)より十分長く遅延させる
            published.append((topic, payload, qos))
            info = MagicMock()
            info.rc = 0
            info.is_published.return_value = True
            return info

        client.publish.side_effect = publish

        logger.log("received", "03", None, "06")
        with caplog.at_level("ERROR", logger="CallLogger"):
            result = logger.flush(timeout=0.1)

    assert result is False
    assert any("flushタイムアウト後も未完了" in r.message for r in caplog.records)

    # スレッドが後始末されるまで待ってから終了する（テスト間の汚染防止）
    for t in list(logger._threads):
        t.join(5.0)

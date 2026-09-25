"""自宅 LAN からの接続用の PIN 認証・LAN の IP アドレス取得・QR コード生成

PIN は起動ごとに secrets で生成し、プロセスのメモリにだけ置く（ファイルやログには出さない）。
総当たり対策として、続けての間違いが上限回数に達したら PIN を作り直し、しばらく入力を受け付けない
"""

from __future__ import annotations

import hmac
import logging
import math
import secrets
import socket
import threading
import time
from collections.abc import Callable
from enum import StrEnum

import qrcode
import qrcode.image.svg

logger = logging.getLogger(__name__)


class PinResult(StrEnum):
    """PIN 照合の結果"""

    OK = 'ok'
    WRONG = 'wrong'
    REGENERATED = 'regenerated'  # 不一致で間違いが上限に達したため PIN を作り直した
    LOCKED = 'locked'  # 間違いが続いたため入力を一時的に受け付けていない


class LanAccess:
    """起動ごとの PIN と、PIN で認証したセッションの照合を管理する（Flask のスレッド間で共有するためロックで保護する）"""

    def __init__(
        self,
        pin_digits: int = 4,
        max_failures: int = 5,
        lockout_seconds: float = 30.0,
        max_lockout_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            pin_digits: PIN の桁数
            max_failures: この回数続けて間違えたら PIN を作り直して入力を一時停止する
            lockout_seconds: 最初の一時停止の秒数（正しい PIN が入力されるまで、作り直すたびに倍にする）
            max_lockout_seconds: 一時停止の上限秒数
            clock: 経過時間の取得関数（テストで差し替える）
        """
        self._pin_digits = pin_digits
        self._max_failures = max_failures
        self._lockout_seconds = lockout_seconds
        self._max_lockout_seconds = max_lockout_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._pin = self._new_pin()
        self._failures = 0
        self._lockouts = 0
        self._locked_until = 0.0
        # セッションに保存する認証済みの印。起動ごとに変わるので、FLASK_SECRET_KEY を固定していても再起動で無効になる
        self.session_token = secrets.token_urlsafe(32)

    def _new_pin(self) -> str:
        """ランダムな PIN を作る（先頭の 0 も桁数に含める）"""
        return f'{secrets.randbelow(10**self._pin_digits):0{self._pin_digits}d}'

    @property
    def pin(self) -> str:
        """現在の PIN（「スマホで開く」画面に表示する）"""
        with self._lock:
            return self._pin

    def locked_seconds(self) -> int:
        """PIN の入力を受け付けない残り秒数（受け付けるなら 0）"""
        with self._lock:
            return self._remaining_lock()

    def _remaining_lock(self) -> int:
        """ロック取得済みの状態で一時停止の残り秒数を返す（端数は切り上げ）"""
        return max(0, math.ceil(self._locked_until - self._clock()))

    def verify(self, submitted: str) -> PinResult:
        """入力された PIN を定数時間で照合する（間違いが上限に達したら PIN を作り直して一時停止し、一致したら回数を戻す）

        Args:
            submitted: フォームから送られた PIN

        Returns:
            OK: 一致 / WRONG: 不一致 / REGENERATED: 不一致で上限に達し PIN を作り直した /
            LOCKED: 一時停止中のため照合していない
        """
        with self._lock:
            if self._remaining_lock():
                return PinResult.LOCKED

            if hmac.compare_digest(submitted.strip().encode('utf-8'), self._pin.encode('ascii')):
                # 間違いの回数と一時停止の段数は正しい PIN が入るまでの分だけ数える（回数は全端末で共通だが、
                # 間に成功を挟んだ散発的な打ち間違いは合算しない）
                self._failures = 0
                self._lockouts = 0
                return PinResult.OK

            self._failures += 1
            logger.warning('lan pin rejected: failures=%d', self._failures)
            if self._failures >= self._max_failures:
                self._pin = self._new_pin()
                self._failures = 0
                self._lockouts += 1
                lockout = min(self._lockout_seconds * 2 ** (self._lockouts - 1), self._max_lockout_seconds)
                self._locked_until = self._clock() + lockout
                logger.warning('lan pin regenerated: reason=too many failures lockout=%ds', lockout)
                return PinResult.REGENERATED

            return PinResult.WRONG

    def is_authenticated(self, token: object) -> bool:
        """セッションの認証済みの印がこの起動のものか確かめる

        Args:
            token: セッションに保存された値（未ログインなら None）
        """
        if not isinstance(token, str):
            return False

        return hmac.compare_digest(token.encode('utf-8'), self.session_token.encode('ascii'))


def lan_ip(probe: tuple[str, int] = ('8.8.8.8', 80)) -> str | None:
    """スマホから接続するための PC の LAN の IP アドレスを返す

    UDP ソケットを外部アドレスへ connect し、OS が選んだ送信元アドレスを読む（UDP の connect はパケットを送らない）

    Args:
        probe: 経路の選択に使う宛先（既定の経路のインターフェースを選ばせるための外部アドレス）

    Returns:
        IP アドレス。ネットワークに接続していないなど取得できなければ None
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(probe)
            address = sock.getsockname()[0]
    except OSError as exc:
        logger.warning('lan ip lookup failed: error=%s', exc)
        return None

    if address.startswith('127.') or address == '0.0.0.0':
        return None

    return address


def qr_svg(data: str) -> str:
    """文字列を QR コードの SVG（HTML に直接埋め込める <svg> 要素）にする

    Args:
        data: QR コードにする文字列（LAN の URL）
    """
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    return image.to_string(encoding='unicode')

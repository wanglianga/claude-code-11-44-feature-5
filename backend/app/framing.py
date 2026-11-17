"""二进制组帧器：维护来源（连接）内的组帧状态。

帧布局（小端）：
  byte0   MAGIC = 0xC7
  byte1   VERSION（协议/固件版本）
  byte2   FLAGS（保留）
  byte3   LEN（载荷字节数，不含头与 CRC）
  4..     载荷 LEN 字节
  末尾    CRC16-CCITT 2 字节（覆盖 byte0..载荷末尾，小端）

规则：
  - 半帧：缓冲等待后续块；连接关闭时仍不足 → truncated。
  - 坏校验：记录 bad_crc 候选帧范围，只跳过一个字节重新扫描，
    保证坏帧不吞掉后面的完整帧。
  - 非法长度：记录 bad_length，跳过一个字节。
  - 帧头前的噪声字节：记录 garbage。
  - 状态按连接隔离，不同连接的残片不串接。
"""
from .crc import crc16_ccitt
from .models import ConnectionState, Frame

MAGIC = 0xC7
HEADER_LEN = 4
CRC_LEN = 2
MAX_PAYLOAD = 64


class Framer:
    def __init__(self, conn: ConnectionState):
        self.conn = conn
        self.buf = bytes(conn.buffer or b"")
        self.base = conn.offset_base
        self.frames: list[Frame] = []

    def _emit(self, start: int, end: int, payload: bytes, status: str, detail: str = ""):
        self.frames.append(Frame(
            batch_id=self.conn.batch_id, connection_id=self.conn.id,
            start_off=start, end_off=end, payload=payload, status=status, detail=detail,
        ))

    def feed(self, chunk: bytes) -> list[Frame]:
        """喂入一个块，返回本次新产生的帧（含坏帧记录）。"""
        before = len(self.frames)
        self.buf += chunk
        self._scan()
        return self.frames[before:]

    def _scan(self):
        while True:
            idx = self.buf.find(bytes([MAGIC]))
            if idx == -1:
                if self.buf:
                    n = len(self.buf)
                    self._emit(self.base, self.base + n, self.buf,
                               "garbage", "帧头魔数前的噪声字节")
                    self.buf = b""
                    self.base += n
                return
            if idx > 0:
                self._emit(self.base, self.base + idx, self.buf[:idx],
                           "garbage", "帧头魔数前的噪声字节")
                self.buf = self.buf[idx:]
                self.base += idx
            if len(self.buf) < HEADER_LEN:
                return  # 半帧头，等下一块
            ver = self.buf[1]
            length = self.buf[3]
            if length > MAX_PAYLOAD or length == 0:
                self._emit(self.base, self.base + 1, self.buf[:1],
                           "bad_length", f"非法载荷长度 {length}（版本 {ver}）")
                self.buf = self.buf[1:]
                self.base += 1
                continue
            total = HEADER_LEN + length + CRC_LEN
            if len(self.buf) < total:
                return  # 半帧，等下一块
            candidate = self.buf[:total]
            body, crc_bytes = candidate[:-CRC_LEN], candidate[-CRC_LEN:]
            want = crc_bytes[0] | (crc_bytes[1] << 8)
            got = crc16_ccitt(body)
            if got == want:
                self._emit(self.base, self.base + total, candidate, "ok",
                           f"version={ver} len={length}")
                self.buf = self.buf[total:]
                self.base += total
            else:
                # 坏校验：记录候选范围，但只前进一个字节重新找魔数，
                # 避免把后面真正的完整帧吞掉。
                self._emit(self.base, self.base + total, candidate, "bad_crc",
                           f"CRC 不符：帧内声明 0x{want:04X}，实算 0x{got:04X}")
                self.buf = self.buf[1:]
                self.base += 1

    def close(self) -> list[Frame]:
        """连接关闭：缓冲区里凑不齐一帧的残片记为截断。"""
        before = len(self.frames)
        if self.buf:
            self._emit(self.base, self.base + len(self.buf), self.buf,
                       "truncated", "连接关闭时剩余的半帧字节")
            self.buf = b""
        return self.frames[before:]

    def state(self) -> tuple[bytes, int]:
        return self.buf, self.base

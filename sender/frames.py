"""二进制帧构造器（发送端独立实现，与服务端无关）。

帧布局（小端）：
  [0] MAGIC 0xC7   [1] VERSION   [2] FLAGS   [3] LEN(载荷长度)
  [4..] 载荷       [末尾2字节] CRC16-CCITT（覆盖头+载荷，小端）

载荷（协议 v1，10 字节）：
  device_no u16 | boot_batch u16 | seq u16 | temp_raw i16 | uptime_s u16
"""
import struct

MAGIC = 0xC7


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def build_frame(device_no: int, boot_batch: int, seq: int, temp_raw: int,
                uptime_s: int = 0, version: int = 1, flags: int = 0,
                corrupt_crc: bool = False) -> bytes:
    payload = struct.pack("<HHHhH", device_no, boot_batch, seq, temp_raw, uptime_s)
    header = bytes([MAGIC, version, flags, len(payload)])
    body = header + payload
    crc = crc16_ccitt(body)
    if corrupt_crc:
        crc ^= 0x00FF  # 故意破坏校验码
    return body + struct.pack("<H", crc)


def temp_c_to_raw(celsius: float) -> int:
    """摄氏 → 定点原始值（scale 0.0625，校准 +0.2 的逆运算仅用于造数）。"""
    return round((celsius + 0.2) / 0.0625)
